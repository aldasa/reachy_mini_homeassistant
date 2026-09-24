"""Mic stream client: answer shaping, lifetime, reconnect, consumers.

The fakes are deliberately dumb (no aiortc): what is under test is the
*session policy* — audio-only answer, no idle teardown, own refcount,
backoff, daemon gate — and that is all expressed in what the client
sends on the signalling socket and how long it keeps it open. The one
thing worth real aiortc is the answer SDP itself, which is negotiated
for real in test_answer_makes_video_inactive_and_audio_recvonly.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

FAKE_OFFER_SDP = (
    "v=0\r\no=- 0 0 IN IP4 172.16.0.170\r\ns=-\r\nt=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:audio0\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 96\r\na=mid:video0\r\n"
)
# Two candidate lines: both must be trickled individually (webrtcbin
# ignores in-SDP candidates), with the m-line index they belong to.
FAKE_ANSWER_SDP = (
    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:audio0\r\n"
    "a=candidate:1 1 UDP 2113937151 127.0.0.1 50000 typ host\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 96\r\na=mid:video0\r\n"
    "a=candidate:2 1 UDP 2113937151 127.0.0.1 50001 typ host\r\n"
)

AUDIO_FRAME_SAMPLES_48K = 960  # 20 ms of 48 kHz stereo, as Opus arrives
SAMPLES_PER_FRAME_16K = 320  # ...resampled to 20 ms of 16 kHz mono s16
# The resampler primes itself: the first output frame is ~16 samples
# short and the rest come out whole, which the assertions allow for.
RESAMPLER_SLACK_SAMPLES = 64


def make_audio_frame(samples: int = AUDIO_FRAME_SAMPLES_48K):
    """A real av.AudioFrame the resampler accepts."""
    import av

    frame = av.AudioFrame(format="s16", layout="stereo", samples=samples)
    frame.sample_rate = 48000
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    return frame


def make_video_frame():
    """A real (tiny, black) av.VideoFrame the JPEG encoder accepts."""
    import av

    frame = av.VideoFrame(16, 16, "rgb24")
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    return frame


class FakeTrack:
    """Stands in for aiortc's RemoteStreamTrack.

    Never raises ``MediaStreamError``: the client cancels the pumping
    task when a session ends (exactly as the camera client does), so the
    tests do not need an end-of-stream signal.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.queue: asyncio.Queue = asyncio.Queue()
        self.recv_calls = 0

    async def recv(self):
        item = await self.queue.get()
        self.recv_calls += 1
        return item


class FakeMediaHub:
    """The robot's audio/video pads, shared across fake sessions.

    A real reconnect hands the client a fresh track; the tests want to
    keep feeding one continuous mic stream across reconnects, so the
    tracks outlive the peer connections that carry them.
    """

    def __init__(self) -> None:
        self.audio = FakeTrack("audio")
        self.video = FakeTrack("video")

    async def feed(self, count: int = 1) -> None:
        for _ in range(count):
            await self.audio.queue.put(make_audio_frame())


class FakeTransceiver:
    """Records the direction the client writes before answering."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._direction = "recvonly"
        self.directions: list[str] = []

    @property
    def direction(self) -> str:
        return self._direction

    @direction.setter
    def direction(self, value: str) -> None:
        self._direction = value
        self.directions.append(value)


class FakeMicPeerConnection:
    """A peer connection that records negotiation and drives tracks."""

    def __init__(self, hub: FakeMediaHub | None = None) -> None:
        self.hub = hub or FakeMediaHub()
        self.audio = self.hub.audio
        self.video = self.hub.video
        self._handlers: dict = {}
        self.remote_description = None
        self.localDescription = type("_Desc", (), {"sdp": FAKE_ANSWER_SDP})()
        self.candidates: list = []
        self.closed = False
        self.transceivers = [FakeTransceiver("audio"), FakeTransceiver("video")]
        self.track_events: list[str] = []

    def on(self, event, handler=None):
        if handler is None:
            def register(fn):
                self._handlers[event] = fn
                return fn

            return register
        self._handlers[event] = handler
        return handler

    async def setRemoteDescription(self, description) -> None:
        self.remote_description = description
        # aiortc fires "track" per negotiated m-line while applying the
        # remote description; a mic session must ignore the video one.
        for track in (self.video, self.audio):
            self.track_events.append(track.kind)
            self._handlers["track"](track)

    async def createAnswer(self):
        return object()

    async def setLocalDescription(self, description) -> None:
        pass

    async def addIceCandidate(self, candidate) -> None:
        self.candidates.append(candidate)

    async def close(self) -> None:
        self.closed = True

    def getTransceivers(self):
        return self.transceivers


class FakeCameraPeerConnection(FakeMicPeerConnection):
    """The camera client's fake: a video m-line, one frame ready to pull."""

    async def setRemoteDescription(self, description) -> None:
        self.remote_description = description
        self.track_events.append("video")
        self._handlers["track"](self.video)
        await self.video.queue.put(make_video_frame())


class FakePCFactory:
    """``pc_factory`` that mints one fake connection per session."""

    def __init__(
        self,
        hub: FakeMediaHub | None = None,
        pc_class: type[FakeMicPeerConnection] = FakeMicPeerConnection,
    ) -> None:
        self.hub = hub or FakeMediaHub()
        self.pc_class = pc_class
        self.created: list[FakeMicPeerConnection] = []

    def __call__(self) -> FakeMicPeerConnection:
        pc = self.pc_class(self.hub)
        self.created.append(pc)
        return pc

    @property
    def current(self) -> FakeMicPeerConnection | None:
        return self.created[-1] if self.created else None

    @property
    def closed(self) -> bool:
        current = self.current
        return current is not None and current.closed

    async def feed(self, count: int = 1) -> None:
        await self.hub.feed(count)


class MicServerScript:
    """Configurable behaviour + transcript for the fake signalling server."""

    def __init__(self) -> None:
        # happy | no_producer | end_session | close_after_answer | stall
        self.behavior = "happy"
        self.received: list[dict] = []
        self.connections = 0
        self.sessions = 0
        self.sockets: list[web.WebSocketResponse] = []

    async def drop_current_session(self) -> None:
        """Close the live socket from the robot side (network died)."""
        for ws in list(self.sockets):
            if not ws.closed:
                await ws.close()


@pytest.fixture
async def mic_server(socket_enabled: None):
    """A scripted gst-webrtc-signalling server for the mic client."""
    script = MicServerScript()

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        if script.behavior == "stall":
            # Never prepares the websocket: the client's ws_connect()
            # await hangs until the caller cancels it.
            await asyncio.sleep(30)
        script.connections += 1
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        script.sockets.append(ws)
        await ws.send_json({"type": "welcome", "peerId": "consumer-1"})
        async for raw in ws:
            msg = json.loads(raw.data)
            script.received.append(msg)
            if msg["type"] == "list":
                producers = (
                    []
                    if script.behavior == "no_producer"
                    else [{"id": "prod-1", "meta": {"name": "reachymini"}}]
                )
                await ws.send_json({"type": "list", "producers": producers})
            elif msg["type"] == "startSession":
                script.sessions += 1
                session_id = f"sess-{script.sessions}"
                await ws.send_json(
                    {
                        "type": "sessionStarted",
                        "peerId": msg["peerId"],
                        "sessionId": session_id,
                    }
                )
                await ws.send_json(
                    {
                        "type": "peer",
                        "sessionId": session_id,
                        "sdp": {"type": "offer", "sdp": FAKE_OFFER_SDP},
                    }
                )
            elif msg["type"] == "peer" and "sdp" in msg:
                if script.behavior == "end_session":
                    await ws.send_json(
                        {"type": "endSession", "sessionId": "sess-1"}
                    )
                elif script.behavior == "close_after_answer":
                    await ws.close()
                else:
                    await ws.send_json(
                        {
                            "type": "peer",
                            "sessionId": f"sess-{script.sessions}",
                            "ice": {
                                "candidate": (
                                    "candidate:1 1 UDP 2015363327 "
                                    "127.0.0.1 48642 typ host"
                                ),
                                "sdpMLineIndex": 0,
                            },
                        }
                    )
        return ws

    app = web.Application()
    app.router.add_get("/", ws_handler)
    server = TestServer(app)
    await server.start_server()
    yield script, server.port
    await server.close()


@pytest.fixture
async def http_session():
    session = aiohttp.ClientSession()
    yield session
    await session.close()


class FakeGate:
    """The daemon-state gate HA will wire to the coordinator."""

    def __init__(self, open_: bool = True) -> None:
        self.open = open_
        self.reads = 0

    def __call__(self) -> bool:
        self.reads += 1
        return self.open


def _make_client(port, http_session, pool, **kwargs):
    from custom_components.reachy_mini.mic_stream import MicStreamClient

    kwargs.setdefault("reconnect_initial", 0.02)
    kwargs.setdefault("reconnect_max", 0.2)
    kwargs.setdefault("reconnect_jitter", 0.0)
    kwargs.setdefault("gate_poll_interval", 0.02)
    return MicStreamClient(
        "127.0.0.1",
        session=http_session,
        port=port,
        pc_factory=pool,
        **kwargs,
    )


async def _wait_for(predicate, timeout=2.0):
    """Poll until predicate() is true (condition-based waiting)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not met"
        await asyncio.sleep(0.01)


class Collector:
    """A consumer that records every chunk it is handed."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    async def __call__(self, pcm: bytes) -> None:
        self.chunks.append(pcm)

    @property
    def bytes_total(self) -> int:
        return sum(len(chunk) for chunk in self.chunks)


# --- interop ---------------------------------------------------------------


async def test_default_pc_factory_installs_interop_certificate() -> None:
    """The mic client needs the same RSA DTLS shim as the camera client.

    Pinned here as well as in test_stream: a future aiortc upgrade that
    makes stream.py's injection unnecessary must not silently leave the
    mic session's handshake broken.
    """
    from custom_components.reachy_mini.mic_stream import _default_pc_factory
    from custom_components.reachy_mini.stream import InteropCertificate

    pc = _default_pc_factory()
    try:
        certs = pc._RTCPeerConnection__certificates
        assert len(certs) == 1
        assert isinstance(certs[0], InteropCertificate)
    finally:
        await pc.close()


async def test_answer_declines_video_without_going_inactive(
    socket_enabled: None,
) -> None:
    """The load-bearing static check for WAKEWORD-PLAN §4 risk 2.

    Answered ``recvonly`` (aiortc's implicit default) the robot encodes
    video for a listener that only wants the mic: 741 video frames in
    25 s, live, 2026-09-23. But ``inactive`` — the obvious way to refuse
    it — never negotiates on this producer at all: the video m-line is
    the BUNDLE transport m-line, its ICE never starts and the session
    dies with no media (producer stuck in ``have-local-offer`` until its
    12 s watchdog). ``sendonly`` is the shape that works: the m-line
    stays active, we receive nothing.
    """
    answer = await _real_answer()
    sections = _sdp_sections(answer)
    assert set(sections) == {"audio", "video"}

    audio_lines = sections["audio"][1]
    video_lines = sections["video"][1]
    assert "a=recvonly" in audio_lines
    assert "a=sendrecv" not in audio_lines
    assert "a=sendonly" in video_lines
    assert "a=inactive" not in video_lines
    assert "a=recvonly" not in video_lines
    # Negotiated, not rejected: m-line port is non-zero.
    assert not sections["video"][0].startswith("m=video 0 ")


async def test_answer_video_direction_is_configurable(
    socket_enabled: None,
) -> None:
    """The escape hatch stays reachable (and tested) for robot builds
    where ``sendonly`` may not hold."""
    from custom_components.reachy_mini.const import (
        MIC_VIDEO_DIRECTION_RECEIVE,
    )

    answer = await _real_answer(video_direction=MIC_VIDEO_DIRECTION_RECEIVE)
    video_lines = _sdp_sections(answer)["video"][1]
    assert "a=recvonly" in video_lines


async def _real_answer(video_direction: str | None = None) -> str:
    """Negotiate a real aiortc offer/answer pair and return our answer."""
    from aiortc import RTCPeerConnection

    from custom_components.reachy_mini.mic_stream import _default_pc_factory

    offerer = RTCPeerConnection()
    pc = _default_pc_factory()
    kwargs = (
        {"video_direction": video_direction} if video_direction else {}
    )
    try:
        offerer.addTrack(FakeMicPeerConnection().video)
        offerer.addTrack(FakeMicPeerConnection().audio)
        await offerer.setLocalDescription(await offerer.createOffer())

        client = _make_client(9, None, FakePCFactory(), **kwargs)
        await pc.setRemoteDescription(offerer.localDescription)
        return await client._create_answer(pc)
    finally:
        await offerer.close()
        await pc.close()


def _sdp_sections(sdp: str) -> dict[str, tuple[str, list[str]]]:
    """Split an SDP into {media: (m-line, following lines)}."""
    sections: dict[str, tuple[str, list[str]]] = {}
    current: str | None = None
    for line in sdp.splitlines():
        if line.startswith("m="):
            current = line.split(" ", 1)[0][2:]
            sections[current] = (line, [])
        elif current is not None:
            sections[current][1].append(line)
    return sections


# --- signalling ------------------------------------------------------------


async def test_happy_path_delivers_pcm_to_consumer(
    mic_server, http_session
) -> None:
    script, port = mic_server
    pool = FakePCFactory()
    client = _make_client(port, http_session, pool)
    collector = Collector()
    client.add_consumer(collector)

    await client.start()
    await _wait_for(lambda: client.connected)
    await pool.feed(2)
    await _wait_for(lambda: len(collector.chunks) == 2)
    await client.async_shutdown()

    # Same documented flow as the camera client, minus the video track.
    types = [m["type"] for m in script.received]
    assert types[0] == "list"
    assert types[1] == "startSession"
    answer = next(m for m in script.received if m["type"] == "peer" and "sdp" in m)
    assert answer["sdp"]["type"] == "answer"
    assert answer["sessionId"] == "sess-1"

    trickled = [m for m in script.received if m["type"] == "peer" and "ice" in m]
    assert len(trickled) == 2
    assert {t["ice"]["sdpMLineIndex"] for t in trickled} == {0, 1}

    # 48 kHz stereo in, 16 kHz mono s16 out.
    assert client.stats.bytes == collector.bytes_total
    assert all(len(chunk) % 2 == 0 for chunk in collector.chunks)
    assert client.stats.samples == pytest.approx(
        2 * SAMPLES_PER_FRAME_16K, abs=RESAMPLER_SLACK_SAMPLES
    )
    assert client.stats.sessions == 1
    assert client.stats.yield_ratio > 0.0
    # connected_seconds is what yield_ratio is measured against: a
    # session that delivered audio must have contributed to it.
    assert client.stats.connected_seconds > 0.0
    assert pool.closed


async def test_video_track_on_mic_session_is_ignored(
    mic_server, http_session
) -> None:
    """A video track must never be pumped on an audio-only session."""
    script, port = mic_server
    pool = FakePCFactory()
    client = _make_client(port, http_session, pool)
    collector = Collector()
    client.add_consumer(collector)

    await client.start()
    await pool.feed(1)
    await _wait_for(lambda: len(collector.chunks) == 1)

    assert pool.current.track_events == ["video", "audio"]  # offered, dropped
    assert pool.hub.audio.recv_calls == 1
    assert pool.hub.video.recv_calls == 0
    await client.async_shutdown()


async def _wait_for_pc(pool: FakePCFactory, predicate, timeout: float = 2.0):
    """Wait until ``predicate`` holds for the pool's *current* session."""
    await _wait_for(
        lambda: (pc := pool.current) is not None and predicate(pc), timeout
    )


async def test_transceivers_shaped_before_answering(
    mic_server, http_session
) -> None:
    """Audio recvonly, everything else inactive — on every transceiver."""
    script, port = mic_server
    pool = FakePCFactory()
    client = _make_client(port, http_session, pool)

    await client.start()
    await _wait_for_pc(pool, lambda pc: pc.remote_description is not None)
    await client.async_shutdown()

    audio, video = pool.current.transceivers
    assert audio.directions == ["recvonly"]
    assert video.directions == ["sendonly"]


async def test_trickled_remote_candidate_reaches_peer(
    mic_server, http_session
) -> None:
    script, port = mic_server
    pool = FakePCFactory()
    client = _make_client(port, http_session, pool)

    await client.start()
    await _wait_for_pc(pool, lambda pc: len(pc.candidates) == 1)
    assert pool.current.candidates[0].sdpMLineIndex == 0
    await client.async_shutdown()


async def test_no_producer_is_a_failed_session_not_a_crash(
    mic_server, http_session
) -> None:
    """``list`` without the producer (daemon asleep) must stay retryable."""
    script, port = mic_server
    script.behavior = "no_producer"
    pool = FakePCFactory()
    client = _make_client(port, http_session, pool, reconnect_max=0.05)

    await client.start()
    await _wait_for(lambda: script.connections >= 2)
    await client.async_shutdown()

    assert client.stats.failed_sessions >= 1
    assert client.stats.reconnects >= 1
    assert client.stats.frames == 0
    assert client.stats.yield_ratio == 0.0
    # It keeps trying: a sleeping robot must not stop the ear for good.
    assert client.stats.sessions >= 2
    # The robot never advertised a producer, so no media was negotiated.
    assert script.sessions == 0


# --- lifetime --------------------------------------------------------------


def test_mic_stream_arms_no_teardown_timer() -> None:
    """No idle/generation timer machinery may leak into the listener.

    The camera client's teardown timer is exactly what would let a
    dashboard closing, or a quiet night, kill the ear. Guard the source
    as well as the behaviour below.
    """
    from custom_components.reachy_mini import mic_stream

    source = Path(mic_stream.__file__).read_text()
    assert "call_later" not in source
    assert "IDLE_TIMEOUT" not in source
    assert "idle_timeout" not in source


async def test_session_survives_zero_consumers(mic_server, http_session) -> None:
    """remove_consumer() must not end the session — no reset, no timer."""
    script, port = mic_server
    pool = FakePCFactory()
    client = _make_client(port, http_session, pool)
    collector = Collector()
    client.add_consumer(collector)

    await client.start()
    await pool.feed(1)
    await _wait_for(lambda: len(collector.chunks) == 1)

    client.remove_consumer(collector)
    assert client.consumers == 0
    assert client.running
    assert client.connected
    await asyncio.sleep(0.15)  # >> the camera's scaled-down idle window
    assert not pool.closed
    assert client.connected

    # The session is still live: frames simply have no consumer for a while.
    other = Collector()
    client.add_consumer(other)
    await pool.feed(1)
    await _wait_for(lambda: len(other.chunks) == 1)
    assert client.stats.sessions == 1  # never renegotiated
    await client.async_shutdown()


async def test_shutdown_stops_session_and_supervisor(
    mic_server, http_session
) -> None:
    script, port = mic_server
    pool = FakePCFactory()
    client = _make_client(port, http_session, pool)

    await client.start()
    await _wait_for_pc(pool, lambda pc: pc.remote_description is not None)
    await client.async_shutdown()

    assert pool.closed
    assert not client.running
    assert not client.connected
    await _wait_for(
        lambda: any(m["type"] == "endSession" for m in script.received)
    )


async def test_consumer_failure_is_isolated(mic_server, http_session) -> None:
    """One broken consumer must not cost the others their audio."""
    script, port = mic_server
    pool = FakePCFactory()
    client = _make_client(port, http_session, pool)

    class Exploding:
        async def __call__(self, pcm: bytes) -> None:
            raise RuntimeError("consumer bug")

    good = Collector()
    client.add_consumer(Exploding())
    client.add_consumer(good)

    await client.start()
    await pool.feed(2)
    await _wait_for(lambda: len(good.chunks) == 2)
    assert client.stats.frames == 2
    await client.async_shutdown()


async def test_consumer_exception_does_not_kill_the_supervisor(
    mic_server, http_session
) -> None:
    """Even a consumer that explodes on every frame keeps the ear up."""
    script, port = mic_server
    pool = FakePCFactory()
    client = _make_client(port, http_session, pool)

    class Exploding:
        async def __call__(self, pcm: bytes) -> None:
            raise RuntimeError("consumer bug")

    client.add_consumer(Exploding())
    await client.start()
    await pool.feed(2)
    await _wait_for(lambda: client.stats.frames == 2)
    assert client.running and client.connected
    assert client.stats.sessions == 1
    await client.async_shutdown()


# --- reconnect -------------------------------------------------------------


async def test_failed_sessions_back_off_geometrically(
    mic_server, http_session
) -> None:
    """A robot that never answers must not be hammered."""
    script, port = mic_server
    script.behavior = "no_producer"
    pool = FakePCFactory()
    client = _make_client(
        port, http_session, pool, reconnect_initial=0.02, reconnect_max=0.16
    )
    delays: list[float] = []

    def record(backoff: float) -> float:
        delays.append(backoff)
        return backoff

    client._jittered = record  # type: ignore[method-assign]
    await client.start()
    await _wait_for(lambda: len(delays) >= 3)
    await client.async_shutdown()

    assert delays[0] == pytest.approx(0.02)
    assert delays[1] == pytest.approx(0.04)
    assert delays[2] == pytest.approx(0.08)
    assert max(delays) <= 0.16


async def test_backoff_resets_after_delivered_audio(
    mic_server, http_session
) -> None:
    """One healthy session (audio flowed) resets the backoff."""
    script, port = mic_server
    script.behavior = "no_producer"
    pool = FakePCFactory()
    client = _make_client(
        port, http_session, pool, reconnect_initial=0.02, reconnect_max=0.16
    )
    delays: list[float] = []

    def record(backoff: float) -> float:
        delays.append(backoff)
        return backoff

    client._jittered = record  # type: ignore[method-assign]
    await client.start()
    await _wait_for(lambda: len(delays) >= 3)
    grown = list(delays)
    assert grown == pytest.approx([0.02, 0.04, 0.08])

    # Robot comes back: the next session delivers frames...
    script.behavior = "happy"
    collector = Collector()
    client.add_consumer(collector)
    await pool.feed(2)
    await _wait_for(lambda: client.stats.frames >= 2, timeout=3.0)

    # ...then the socket dies again: the retry is back at the floor.
    await script.drop_current_session()
    await _wait_for(lambda: len(delays) > len(grown), timeout=3.0)
    await client.async_shutdown()

    assert delays[len(grown)] == pytest.approx(0.02)


async def test_force_disconnect_reconnects_immediately(
    mic_server, http_session
) -> None:
    """The 'cable yanked' path: drop the socket, renegotiate at once."""
    script, port = mic_server
    pool = FakePCFactory()
    client = _make_client(port, http_session, pool)
    delays: list[float] = []

    def record(backoff: float) -> float:
        delays.append(backoff)
        return backoff

    client._jittered = record  # type: ignore[method-assign]
    collector = Collector()
    client.add_consumer(collector)

    await client.start()
    await pool.feed(1)
    await _wait_for(lambda: len(collector.chunks) == 1)

    await client.force_disconnect()
    await _wait_for(lambda: script.sessions >= 2)
    assert delays == []  # a deliberate drop never backs off
    assert client.stats.reconnects >= 1

    # ...and audio flows again on the fresh session.
    await pool.feed(1)
    await _wait_for(lambda: len(collector.chunks) == 2)
    assert client.stats.sessions >= 2
    await client.async_shutdown()


async def test_gaps_are_recorded_when_frames_stall(
    mic_server, http_session
) -> None:
    script, port = mic_server
    pool = FakePCFactory()
    client = _make_client(port, http_session, pool)
    collector = Collector()
    client.add_consumer(collector)

    await client.start()
    await pool.feed(1)
    await _wait_for(lambda: len(collector.chunks) == 1)
    await asyncio.sleep(0.15)  # ~7 missing 20 ms frames
    await pool.feed(1)
    await _wait_for(lambda: len(collector.chunks) == 2)

    gaps = client.stats.gaps
    assert len(gaps) == 1
    assert gaps[0]["missing_frames"] >= 5
    assert gaps[0]["seconds"] > 0.1
    await client.async_shutdown()


async def test_stats_yield_ratio_matches_delivered_samples() -> None:
    from custom_components.reachy_mini.mic_stream import MicStreamStats

    stats = MicStreamStats()
    stats.connected_seconds = 60.0
    stats.samples = 16000 * 57  # 3 s lost in a 60 s window
    assert stats.expected_samples == 960000
    assert stats.yield_ratio == pytest.approx(0.95)
    # A session that never delivered anything reports zero, not 100%.
    empty = MicStreamStats()
    assert empty.yield_ratio == 0.0


async def test_unreachable_robot_retries_without_hammering(
    socket_enabled: None, http_session
) -> None:
    """Nothing listening (daemon stopped): retry, but on a backoff."""
    from custom_components.reachy_mini.mic_stream import MicStreamClient

    client = MicStreamClient(
        "127.0.0.1",
        session=http_session,
        port=1,  # nothing listens here
        pc_factory=FakePCFactory(),
        reconnect_initial=0.05,
        reconnect_max=0.1,
        reconnect_jitter=0.0,
    )
    await client.start()
    await asyncio.sleep(0.45)
    await client.async_shutdown()

    assert client.stats.failed_sessions >= 2
    # 0.45 s at >= 0.05 s spacing cannot be more than ~10 attempts; the
    # point is that it is bounded, not a hot loop.
    assert client.stats.sessions <= 10


# --- daemon gate -----------------------------------------------------------


async def test_gate_closed_makes_no_connection_attempt(
    mic_server, http_session
) -> None:
    script, port = mic_server
    pool = FakePCFactory()
    gate = FakeGate(open_=False)
    client = _make_client(port, http_session, pool, gate=gate)

    await client.start()
    await asyncio.sleep(0.15)
    assert script.connections == 0
    assert script.received == []
    assert client.stats.sessions == 0

    gate.open = True
    await _wait_for(lambda: script.connections == 1)
    await client.async_shutdown()


async def test_gate_close_drops_session_and_waits(
    mic_server, http_session
) -> None:
    """daemon_state leaves 'running' -> session closed, no reconnect churn."""
    script, port = mic_server
    pool = FakePCFactory()
    gate = FakeGate(open_=True)
    client = _make_client(port, http_session, pool, gate=gate)
    collector = Collector()
    client.add_consumer(collector)

    await client.start()
    await pool.feed(1)
    await _wait_for(lambda: len(collector.chunks) == 1)

    connections_before = script.connections
    gate.open = False
    await _wait_for(lambda: pool.closed)
    await _wait_for(lambda: client.stats.gate_closed_seconds > 0.0)
    await asyncio.sleep(0.15)  # plenty of gate polls
    assert script.connections == connections_before  # no retry while down

    # Waking the robot brings the ear straight back.
    gate.open = True
    await _wait_for(lambda: script.connections == connections_before + 1)
    assert client.stats.sessions >= 2
    await client.async_shutdown()


# --- independence from the camera client -----------------------------------


async def test_camera_release_does_not_touch_the_mic_session(
    mic_server, http_session
) -> None:
    """Session independence: the ear must outlive the eye.

    Both clients share one robot (and one signalling endpoint) but must
    own their sessions: the camera's idle teardown at zero consumers is
    exactly the event that must not end the listener.
    """
    from custom_components.reachy_mini.stream import ReachyMiniStreamClient

    script, port = mic_server
    mic_pool = FakePCFactory()
    camera_pool = FakePCFactory(pc_class=FakeCameraPeerConnection)

    mic = _make_client(port, http_session, mic_pool)
    camera = ReachyMiniStreamClient(
        "127.0.0.1",
        session=http_session,
        port=port,
        pc_factory=camera_pool,
        idle_timeout=0.05,
    )
    collector = Collector()
    mic.add_consumer(collector)

    await mic.start()
    await camera.acquire()
    await mic_pool.feed(1)
    await _wait_for(lambda: len(collector.chunks) == 1)
    assert script.sessions == 2  # two independent sessions on one robot

    await camera.release()
    await _wait_for(lambda: camera_pool.closed)  # camera session torn down

    assert not mic_pool.closed
    assert mic.connected
    await mic_pool.feed(1)
    await _wait_for(lambda: len(collector.chunks) == 2)

    # ...and the reverse: closing the ear leaves the camera's session alone.
    sessions_before = len(camera_pool.created)
    await camera.acquire()
    # Wait for the *new* session, not merely for "a" session: the torn
    # down one is still the pool's current entry until the task runs.
    await _wait_for(
        lambda: (
            len(camera_pool.created) > sessions_before
            and camera_pool.current.remote_description is not None
        )
    )
    assert not camera_pool.closed
    await mic.async_shutdown()
    assert not camera_pool.closed
    assert camera._task is not None and not camera._task.done()
    await camera.release()
    await _wait_for(lambda: camera_pool.closed)
