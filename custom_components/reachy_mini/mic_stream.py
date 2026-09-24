"""Audio-only WebRTC consumer for the Reachy Mini microphone.

Same wire protocol as the camera client (:mod:`.stream`) — the daemon's
``webrtcsink`` runs a gst-webrtc-signalling server on
``ws://<host>:8443`` that only supports the *producer-offers* flow, the
robot's gst build presents an RSA DTLS certificate, and in-SDP ICE
candidates are ignored — but a deliberately different lifetime:

- **Audio only.** The answer makes every non-audio m-line ``inactive``.
  Answering the video m-line ``recvonly`` (aiortc's implicit default)
  would have the CM4 software-encode 720p for a listener that only wants
  the mic: that is WAKEWORD-PLAN.md §4 risk 2, and it is the whole point
  of keeping this session separate from the camera's.
- **No idle teardown.** The camera client tears its session down 10 s
  after the last dashboard consumer because camera demand is bursty; the
  ear has no consumers in that sense and must simply stay up.
- **Own refcount.** HA consumers (a detector feed, a level meter, a
  "listen" switch) register here, never on the camera client, so camera
  availability and listener availability cannot take each other out.
- **Reconnect forever, with backoff.** A dropped socket, a restarted
  daemon or a yanked cable all end up in the same supervisor loop.
- **Gated on ``daemon_state == running``.** While the backend is down
  there is no producer to talk to, so the gate stops the listener from
  filling the robot's logs with connection attempts (and makes the
  live test's sleep/wake flip observable as gate-open/closed).

Decoded audio is broadcast to consumers registered with
:meth:`MicStreamClient.add_consumer` — async callables taking one chunk
of 16 kHz mono s16 PCM. Consumers run inline on the receive path, so
they must be quick (a WAV write, a ring-buffer write, a queue put).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import candidate_from_sdp

from .const import (
    MIC_FRAME_MS,
    MIC_GAP_TOLERANCE,
    MIC_GATE_POLL_INTERVAL,
    MIC_RECONNECT_FACTOR,
    MIC_RECONNECT_INITIAL,
    MIC_RECONNECT_JITTER,
    MIC_RECONNECT_MAX,
    MIC_SAMPLE_RATE,
    MIC_VIDEO_DIRECTION,
    PRODUCER_NAME,
    SIGNALLING_PORT,
)
from .stream import InteropCertificate

_LOGGER = logging.getLogger(__name__)

# A consumer is an async callable fed one PCM chunk per decoded frame.
MicConsumer = Callable[[bytes], Awaitable[None]]


class MicStreamError(Exception):
    """The robot's mic stream cannot be reached right now."""


def _default_pc_factory() -> RTCPeerConnection:
    """A peer connection carrying the robot's DTLS interop shim.

    Mirrors ``stream._default_pc_factory``: aiortc's ``RTCConfiguration``
    has no ``certificates`` field, so the RSA-capable certificate has to
    be installed on the private attribute. test_mic_stream asserts this
    factory installs an :class:`InteropCertificate`, so a future aiortc
    that gains the public field cannot silently drop the shim.
    """
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc._RTCPeerConnection__certificates = [InteropCertificate.generate()]
    return pc


class MicStreamStats:
    """Counters the live harness (and, later, HA sensors) read.

    ``yield_ratio`` is decoded samples over ``rate * connected_seconds``,
    where ``connected_seconds`` only accumulates inside sessions that
    actually delivered audio. That is deliberate: a robot that is asleep
    or restarting for ten minutes must not read as *capture loss*, it is
    a separate fact (``gate_closed_seconds`` / ``reconnects``). Wall-clock
    yield — samples over the harness's own elapsed time — is computed by
    the harness from ``samples`` and its own clock.
    """

    def __init__(
        self,
        *,
        sample_rate: int = MIC_SAMPLE_RATE,
        frame_ms: int = MIC_FRAME_MS,
    ) -> None:
        """Create zeroed counters for one client."""
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.sessions = 0
        self.reconnects = 0
        self.failed_sessions = 0
        self.frames = 0
        self.samples = 0
        self.bytes = 0
        self.connected_seconds = 0.0
        self.gate_closed_seconds = 0.0
        self.gaps: list[dict[str, Any]] = []
        self.last_frame_at: float | None = None

    @property
    def frame_seconds(self) -> float:
        """Nominal duration of one decoded frame."""
        return self.frame_ms / 1000.0

    @property
    def expected_samples(self) -> int:
        """Samples a perfectly fed capture would have produced."""
        return int(self.sample_rate * self.connected_seconds)

    @property
    def yield_ratio(self) -> float:
        """Delivered samples / expected samples inside live sessions."""
        expected = self.expected_samples
        if expected <= 0:
            return 0.0
        return min(1.0, self.samples / expected)

    def note_gap(self, seconds: float, missing_frames: int) -> None:
        """Record a hole in the stream (stall, jitter spike, decoder lag)."""
        self.gaps.append(
            {
                "at": self.last_frame_at,
                "seconds": round(seconds, 4),
                "missing_frames": missing_frames,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly snapshot for logs and results files."""
        return {
            "sessions": self.sessions,
            "reconnects": self.reconnects,
            "failed_sessions": self.failed_sessions,
            "frames": self.frames,
            "samples": self.samples,
            "bytes": self.bytes,
            "connected_seconds": round(self.connected_seconds, 3),
            "gate_closed_seconds": round(self.gate_closed_seconds, 3),
            "expected_samples": self.expected_samples,
            "yield_ratio": round(self.yield_ratio, 5),
            "gaps": list(self.gaps),
            "last_frame_at": self.last_frame_at,
        }


class MicStreamClient:
    """One always-on audio-only session against one robot.

    ``gate`` is a zero-argument callable answering "is the daemon up?".
    HA passes the coordinator's ``daemon_state`` check; the standalone
    harness passes a REST poll. ``None`` means "always open".
    """

    def __init__(
        self,
        host: str,
        *,
        session: aiohttp.ClientSession,
        port: int = SIGNALLING_PORT,
        pc_factory: Callable[[], Any] | None = None,
        gate: Callable[[], bool] | None = None,
        gate_poll_interval: float = MIC_GATE_POLL_INTERVAL,
        reconnect_initial: float = MIC_RECONNECT_INITIAL,
        reconnect_max: float = MIC_RECONNECT_MAX,
        reconnect_factor: float = MIC_RECONNECT_FACTOR,
        reconnect_jitter: float = MIC_RECONNECT_JITTER,
        sample_rate: int = MIC_SAMPLE_RATE,
        video_direction: str = MIC_VIDEO_DIRECTION,
    ) -> None:
        """Bind to one robot; ``pc_factory`` is injectable for tests.

        ``video_direction`` is the answer direction for the non-audio
        m-line(s); see :data:`.const.MIC_VIDEO_DIRECTION` for why it is
        ``sendonly`` rather than ``inactive``.
        """
        self._host = host
        self._port = port
        self._session = session
        self._pc_factory = pc_factory or _default_pc_factory
        self._gate = gate
        self._gate_poll_interval = gate_poll_interval
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._reconnect_factor = reconnect_factor
        self._reconnect_jitter = reconnect_jitter
        self._sample_rate = sample_rate
        self._video_direction = video_direction
        self.stats = MicStreamStats(sample_rate=sample_rate)
        self._consumers: list[MicConsumer] = []
        self._supervisor: asyncio.Task | None = None
        self._audio_task: asyncio.Task | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pc: Any | None = None
        self._session_id: str | None = None
        self._stopping = False
        # Reasons observed by the signalling loop, translated into the
        # supervisor's exit reason. Both are set while a session is up
        # and consumed by _run_session's finally.
        self._forced_disconnect = False
        self._gate_closed = False
        self._session_first_frame_at: float | None = None

    # -- introspection --------------------------------------------------

    @property
    def signalling_url(self) -> str:
        """gst-webrtc-signalling endpoint on the robot."""
        return f"ws://{self._host}:{self._port}"

    @property
    def consumers(self) -> int:
        """Number of registered consumers (the client's own refcount)."""
        return len(self._consumers)

    @property
    def running(self) -> bool:
        """True while the reconnect supervisor is alive."""
        return self._supervisor is not None and not self._supervisor.done()

    @property
    def connected(self) -> bool:
        """True while a signalling session is established."""
        return self._ws is not None

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Start the supervisor (idempotent)."""
        if self.running:
            return
        self._stopping = False
        self._supervisor = asyncio.get_running_loop().create_task(
            self._supervise()
        )

    async def async_shutdown(self) -> None:
        """Stop listening and close the session immediately.

        Bound and passed to ``entry.async_on_unload`` by the (future)
        HA wiring, same as the camera client.
        """
        self._stopping = True
        task, self._supervisor = self._supervisor, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # A session started outside the supervisor (or one whose cancel
        # raced the supervisor's own teardown) must not outlive us.
        await self._close_media()

    def add_consumer(self, consumer: MicConsumer) -> None:
        """Register an async PCM consumer (own refcount).

        No teardown is armed when the last consumer leaves: the client's
        lifetime is ``start()``/``async_shutdown()``, not refcount — that
        is the difference from the camera client in one line.
        """
        if not callable(consumer):
            raise TypeError("consumer must be an async callable")
        self._consumers.append(consumer)

    def remove_consumer(self, consumer: MicConsumer) -> None:
        """Deregister one consumer instance (idempotent)."""
        with contextlib.suppress(ValueError):
            self._consumers.remove(consumer)

    async def force_disconnect(self) -> None:
        """Drop the live socket without a polite ``endSession``.

        The "network died" path: the robot's producer keeps its session
        until its own timeout, exactly like a yanked cable, so the
        supervisor has to notice and renegotiate. Used by the live
        harness to prove reconnect against the real robot, and by the
        static tests.
        """
        self._forced_disconnect = True
        ws = self._ws
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close()

    # -- supervisor -----------------------------------------------------

    def _gate_open(self) -> bool:
        return self._gate is None or bool(self._gate())

    async def _supervise(self) -> None:
        """Connect, reconnect, and never leave the listener down.

        Exit reasons from :meth:`_run_session`:

        - ``force_disconnect`` — deliberate, reconnect instantly (the
          harness asks for it; a human just cut the socket).
        - anything else — back off, doubling to ``reconnect_max`` with
          jitter. A session that delivered audio resets the backoff, so
          a flapping daemon still recovers fast.
        """
        backoff = self._reconnect_initial
        try:
            while True:
                if not self._gate_open():
                    await self._wait_for_gate()
                    continue
                frames_before = self.stats.frames
                reason = await self._run_session()
                if reason == "force_disconnect":
                    continue
                delivered = self.stats.frames > frames_before
                if delivered:
                    backoff = self._reconnect_initial
                else:
                    self.stats.failed_sessions += 1
                if self._stopping:
                    return
                # Wait the *current* backoff, then grow it: the first
                # retry after a loss is as fast as the first one.
                delay = backoff
                if not delivered:
                    backoff = min(
                        backoff * self._reconnect_factor, self._reconnect_max
                    )
                await asyncio.sleep(self._jittered(delay))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pragma: no cover - supervisor bug guard
            _LOGGER.exception("Reachy Mini mic supervisor died: %s", err)

    def _jittered(self, backoff: float) -> float:
        """Spread simultaneous reconnects (HA restart, robot reboot)."""
        if self._reconnect_jitter <= 0:
            return backoff
        return backoff * (1 + self._reconnect_jitter * random.random())

    async def _wait_for_gate(self) -> None:
        """Idle while the daemon is down, counting the downtime.

        Accumulated per poll rather than once at the end, so the counter
        is readable *while* the gate is still shut (the live harness and
        the sleep/wake test both read it mid-downtime).
        """
        while not self._stopping and not self._gate_open():
            started = time.monotonic()
            await asyncio.sleep(self._gate_poll_interval)
            self.stats.gate_closed_seconds += time.monotonic() - started

    async def _run_session(self) -> str:
        """One signalling session, from connect to close.

        Returns the exit reason; never raises except on cancellation.
        """
        pc = self._pc_factory()
        self._pc = pc
        self._session_id = None
        self._forced_disconnect = False
        self._gate_closed = False
        self._session_first_frame_at = None
        self.stats.sessions += 1
        if self.stats.sessions > 1:
            self.stats.reconnects += 1
        reason = "unknown"
        try:
            async with self._session.ws_connect(
                self.signalling_url, heartbeat=20
            ) as ws:
                self._ws = ws
                watcher = asyncio.get_running_loop().create_task(
                    self._gate_watch(ws)
                )
                try:
                    reason = await self._signalling_loop(ws, pc)
                finally:
                    watcher.cancel()
                    with contextlib.suppress(
                        asyncio.CancelledError, Exception
                    ):
                        await watcher
                    # Polite teardown mirrors the SDK clients; after a
                    # cancellation it may already be gone.
                    if self._session_id is not None and not ws.closed:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                ws.send_json(
                                    {
                                        "type": "endSession",
                                        "sessionId": self._session_id,
                                    }
                                ),
                                1,
                            )
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, OSError, ValueError) as err:
            reason = "error"
            _LOGGER.debug("Reachy Mini mic session failed: %s", err)
        finally:
            self._ws = None
            first = self._session_first_frame_at
            if first is not None:
                self.stats.connected_seconds += time.monotonic() - first
            await self._close_media()
        if self._forced_disconnect:
            return "force_disconnect"
        if self._gate_closed:
            return "gate_closed"
        _LOGGER.debug("Reachy Mini mic session ended: %s", reason)
        return reason

    async def _gate_watch(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Close the session as soon as the daemon leaves ``running``."""
        while True:
            await asyncio.sleep(self._gate_poll_interval)
            if not self._gate_open():
                self._gate_closed = True
                _LOGGER.debug("daemon_state left 'running' — mic session closed")
                with contextlib.suppress(Exception):
                    await ws.close()
                return

    async def _close_media(self) -> None:
        """Cancel the audio pump and close the peer connection."""
        task, self._audio_task = self._audio_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        pc, self._pc = self._pc, None
        if pc is not None:
            with contextlib.suppress(Exception):
                await pc.close()

    # -- signalling -----------------------------------------------------

    async def _signalling_loop(self, ws: Any, pc: Any) -> str:
        """Drive the producer-offers handshake; return the exit reason."""
        pc.on("track", self._on_track)
        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                return "socket_closed"
            msg = json.loads(raw.data)
            mtype = msg.get("type")
            if mtype == "welcome":
                await ws.send_json({"type": "list"})
            elif mtype == "list":
                producer_id = next(
                    (
                        p["id"]
                        for p in msg.get("producers", [])
                        if p.get("meta", {}).get("name") == PRODUCER_NAME
                    ),
                    None,
                )
                if producer_id is None:
                    _LOGGER.warning(
                        "Reachy Mini mic producer %r not advertised "
                        "(daemon_state != running, or media held exclusively?)",
                        PRODUCER_NAME,
                    )
                    return "no_producer"
                await ws.send_json(
                    {"type": "startSession", "peerId": producer_id}
                )
            elif mtype == "sessionStarted":
                self._session_id = msg["sessionId"]
            elif mtype == "peer" and "sdp" in msg:
                await pc.setRemoteDescription(
                    RTCSessionDescription(msg["sdp"]["sdp"], msg["sdp"]["type"])
                )
                sdp = await self._create_answer(pc)
                await ws.send_json(
                    {
                        "type": "peer",
                        "sessionId": self._session_id,
                        "sdp": {"type": "answer", "sdp": sdp},
                    }
                )
                # gst webrtcbin ignores in-SDP candidates: trickle each.
                mline = -1
                for line in sdp.splitlines():
                    if line.startswith("m="):
                        mline += 1
                    elif line.startswith("a=candidate:"):
                        await ws.send_json(
                            {
                                "type": "peer",
                                "sessionId": self._session_id,
                                "ice": {
                                    "candidate": line[2:],
                                    "sdpMLineIndex": mline,
                                },
                            }
                        )
            elif mtype == "peer" and "ice" in msg:
                ice = msg["ice"] or {}
                cand = ice.get("candidate")
                if cand:
                    candidate = candidate_from_sdp(
                        cand.replace("candidate:", "", 1)
                    )
                    candidate.sdpMLineIndex = ice.get("sdpMLineIndex", 0)
                    await pc.addIceCandidate(candidate)
            elif mtype == "endSession":
                _LOGGER.debug("Producer ended the mic session")
                return "end_session"
        return "socket_closed"

    async def _create_answer(self, pc: Any) -> str:
        """Answer the robot's offer: audio ``recvonly``, video not received.

        aiortc creates an implicit *receiving* transceiver for every
        m-line the robot offers, so an untouched answer would have the
        CM4 encode video for a listener that only wants the mic — and
        ``inactive`` (the obvious way to refuse it) never negotiates at
        all on this producer, because the video m-line carries the
        BUNDLE transport. See :data:`.const.MIC_VIDEO_DIRECTION` for the
        measurements behind ``sendonly``. The answer SDP is asserted in
        test_mic_stream.
        """
        for transceiver in pc.getTransceivers():
            if transceiver.kind == "audio":
                transceiver.direction = "recvonly"
            else:
                transceiver.direction = self._video_direction
        await pc.setLocalDescription(await pc.createAnswer())
        return pc.localDescription.sdp

    # -- media ----------------------------------------------------------

    def _on_track(self, track: Any) -> None:
        """Route the audio track to the resampler; ignore everything else."""
        if track.kind != "audio":
            _LOGGER.debug(
                "Ignoring %s track on the mic session (answered %s)",
                track.kind,
                self._video_direction,
            )
            return
        if self._audio_task is None:
            self._audio_task = asyncio.get_running_loop().create_task(
                self._consume_audio(track)
            )

    async def _consume_audio(self, track: Any) -> None:
        """Decode Opus to 16 kHz mono s16 and fan it out to consumers."""
        import av

        resampler = av.AudioResampler(
            format="s16", layout="mono", rate=self._sample_rate
        )
        loop = asyncio.get_running_loop()
        previous: float | None = None
        try:
            while True:
                frame = await track.recv()
                now = loop.time()
                if self._session_first_frame_at is None:
                    self._session_first_frame_at = now
                elif previous is not None:
                    delta = now - previous
                    threshold = self.stats.frame_seconds * MIC_GAP_TOLERANCE
                    if delta > threshold:
                        missing = int(round(delta / self.stats.frame_seconds)) - 1
                        self.stats.note_gap(delta, max(missing, 1))
                previous = now
                for resampled in resampler.resample(frame):
                    # to_ndarray().tobytes() rather than planes[0]: a strided
                    # plane would silently pad the frame.
                    pcm = resampled.to_ndarray().tobytes()
                    self.stats.frames += 1
                    self.stats.samples += resampled.samples
                    self.stats.bytes += len(pcm)
                    self.stats.last_frame_at = now
                    await self._dispatch(pcm)
        except MediaStreamError:
            _LOGGER.debug("Reachy Mini mic track ended")

    async def _dispatch(self, pcm: bytes) -> None:
        """Feed every registered consumer, isolating their failures."""
        for consumer in list(self._consumers):
            try:
                await consumer(pcm)
            except Exception as err:
                _LOGGER.warning(
                    "Mic consumer %r failed: %s", consumer, err, exc_info=err
                )
