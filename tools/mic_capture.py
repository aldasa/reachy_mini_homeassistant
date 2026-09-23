#!/usr/bin/env python3
"""Live P1 harness: capture the Reachy Mini mic to WAV over MicStreamClient.

This is the throwaway script the fork's MicStreamClient was proven with
before any HA wiring exists (WAKEWORD-PLAN.md §1, P1). It runs on the
gateway/HA host, talks to the robot's gst-webrtc-signalling server, and
writes:

- ``--wav``          16 kHz mono s16 PCM, straight from the decoder
- ``--stats``        JSON: frame/sample counts, yield, gaps, reconnects
- ``--events``       JSONL timeline: every gate transition, camera probe,
                     forced disconnect, mark — so the HA-side button
                     presses can be matched to what the stream did

The daemon-state gate is a REST poll of ``/api/daemon/status`` (the
standalone stand-in for the coordinator's ``daemon_state``), which also
means the sleep/wake flip is recorded from the robot's own answer rather
than inferred from dropped frames.

Usage (from the fork checkout):

    .venv/bin/python tools/mic_capture.py --host 192.168.1.214 --seconds 60

Nothing here writes to the robot: it only joins the producer as a
consumer (that is the whole point of route C).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import resource
import signal
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import aiohttp  # noqa: E402

from custom_components.reachy_mini.mic_audio import MicDumpConsumer  # noqa: E402
from custom_components.reachy_mini.mic_stream import MicStreamClient  # noqa: E402
from custom_components.reachy_mini.stream import (  # noqa: E402
    ReachyMiniStreamClient,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="robot host/IP")
    parser.add_argument("--signalling-port", type=int, default=8443)
    parser.add_argument("--rest-port", type=int, default=8000)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--wav", required=True)
    parser.add_argument("--stats", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--ring-seconds", type=float, default=5.0)
    parser.add_argument(
        "--timeline",
        help="JSON file: [{\"at\": <seconds>, \"action\": ...}]",
    )
    parser.add_argument(
        "--progress-every", type=float, default=30.0, help="log cadence"
    )
    return parser.parse_args()


class EventLog:
    """Timeline of everything the harness observed or did."""

    def __init__(self, path: Path, started: float) -> None:
        self.path = path
        self.started = started
        self.events: list[dict] = []
        self._handle = path.open("w")

    def add(self, kind: str, **fields) -> None:
        entry = {
            "t": round(time.monotonic() - self.started, 3),
            "wall": time.strftime("%H:%M:%S"),
            "kind": kind,
            **fields,
        }
        self.events.append(entry)
        self._handle.write(json.dumps(entry) + "\n")
        self._handle.flush()
        print(f"[{entry['wall']} +{entry['t']:7.1f}s] {kind} {fields}", flush=True)

    def close(self) -> None:
        self._handle.close()


class DaemonGate:
    """REST-polled daemon-state gate (stand-in for coordinator data)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        log: EventLog,
        interval: float = 1.0,
    ) -> None:
        self._session = session
        self._url = url
        self._log = log
        self._interval = interval
        self.state: str | None = None
        self.motor_mode: str | None = None
        self.control_loop_hz: float | None = None
        self._task: asyncio.Task | None = None

    def __call__(self) -> bool:
        return self.state == "running"

    async def _read(self) -> dict | None:
        try:
            async with self._session.get(
                self._url, timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception:
            return None

    async def watch(self) -> None:
        """Poll the daemon; log every transition with its cause."""
        while True:
            payload = await self._read()
            if payload is None:
                state, motor, hz = "unreachable", None, None
            else:
                state = payload.get("state")
                backend = payload.get("backend_status") or {}
                motor = backend.get("motor_control_mode")
                stats = backend.get("control_loop_stats") or {}
                hz = stats.get("mean_control_loop_frequency")
            if state != self.state:
                self._log.add(
                    "daemon_state",
                    state=state,
                    previous=self.state,
                    motor_control_mode=motor,
                )
            self.state = state
            self.motor_mode = motor
            self.control_loop_hz = hz
            await asyncio.sleep(self._interval)

    async def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self.watch())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


async def camera_probe(
    host: str,
    port: int,
    session: aiohttp.ClientSession,
    log: EventLog,
    count: int,
    label: str,
) -> None:
    """Open/close full camera sessions while the mic session runs.

    This is the session-independence test: the camera client has its own
    refcount and idle teardown, so the mic stream's continuity across
    these cycles is the evidence (WAKEWORD-PLAN.md §3 P1).
    """
    client = ReachyMiniStreamClient(host, session=session, port=port)
    try:
        for index in range(1, count + 1):
            await client.acquire()
            try:
                started = time.monotonic()
                jpeg = await client.async_get_image(timeout=10)
                log.add(
                    "camera_probe",
                    label=label,
                    cycle=index,
                    bytes=len(jpeg),
                    latency_ms=round((time.monotonic() - started) * 1000),
                )
            except Exception as err:
                log.add(
                    "camera_probe_failed",
                    label=label,
                    cycle=index,
                    error=f"{type(err).__name__}: {err}",
                )
            finally:
                await client.release()
            await asyncio.sleep(0.2)
    finally:
        await client.async_shutdown()


async def run_timeline(
    path: Path,
    host: str,
    port: int,
    session: aiohttp.ClientSession,
    client: MicStreamClient,
    log: EventLog,
) -> None:
    """Execute the scripted events on their offsets from capture start."""
    schedule = json.loads(path.read_text())
    started = time.monotonic()
    for item in sorted(schedule, key=lambda entry: entry["at"]):
        delay = item["at"] - (time.monotonic() - started)
        if delay > 0:
            await asyncio.sleep(delay)
        action = item["action"]
        if action == "camera_probe":
            await camera_probe(
                host,
                port,
                session,
                log,
                int(item.get("count", 1)),
                item.get("label", "camera"),
            )
        elif action == "force_disconnect":
            log.add("force_disconnect", note=item.get("note"))
            await client.force_disconnect()
        elif action == "mark":
            log.add("mark", label=item.get("label"))
        else:
            log.add("unknown_action", action=action)


async def main() -> int:
    args = parse_args()
    started = time.monotonic()
    log = EventLog(Path(args.events), started)
    wav_path = Path(args.wav)
    stats_path = Path(args.stats)
    consumer = MicDumpConsumer(wav_path, ring_seconds=args.ring_seconds)
    usage_before = resource.getrusage(resource.RUSAGE_SELF)

    async with aiohttp.ClientSession() as session:
        gate = DaemonGate(
            session,
            f"http://{args.host}:{args.rest_port}/api/daemon/status",
            log,
        )
        await gate.start()
        client = MicStreamClient(
            args.host,
            session=session,
            port=args.signalling_port,
            gate=gate,
            gate_poll_interval=1.0,
        )
        client.add_consumer(consumer)
        log.add(
            "capture_start",
            host=args.host,
            seconds=args.seconds,
            wav=str(wav_path),
            gate="daemon_state==running",
        )

        timeline_task = None
        if args.timeline:
            timeline_task = asyncio.get_running_loop().create_task(
                run_timeline(
                    Path(args.timeline),
                    args.host,
                    args.signalling_port,
                    session,
                    client,
                    log,
                )
            )

        await client.start()
        deadline = started + args.seconds
        stop = False

        def _stop(*_: object) -> None:
            nonlocal stop
            stop = True

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _stop)

        last_progress = started
        while time.monotonic() < deadline and not stop:
            await asyncio.sleep(0.5)
            if time.monotonic() - last_progress >= args.progress_every:
                last_progress = time.monotonic()
                elapsed = time.monotonic() - started
                stats = client.stats
                log.add(
                    "progress",
                    elapsed=round(elapsed, 1),
                    wav_seconds=round(consumer.duration, 2),
                    frames=stats.frames,
                    sessions=stats.sessions,
                    reconnects=stats.reconnects,
                    yield_connected=round(stats.yield_ratio, 4),
                    wall_yield=round(
                        consumer.duration / elapsed if elapsed else 0.0, 4
                    ),
                    gate_closed_seconds=round(stats.gate_closed_seconds, 1),
                    daemon_state=gate.state,
                    control_loop_hz=(
                        round(gate.control_loop_hz, 2)
                        if gate.control_loop_hz
                        else None
                    ),
                )

        if timeline_task is not None:
            timeline_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timeline_task
        await client.async_shutdown()
        await gate.stop()
        consumer.close()

    elapsed = time.monotonic() - started
    usage_after = resource.getrusage(resource.RUSAGE_SELF)
    cpu_seconds = (usage_after.ru_utime - usage_before.ru_utime) + (
        usage_after.ru_stime - usage_before.ru_stime
    )
    stats = client.stats
    summary = {
        "host": args.host,
        "requested_seconds": args.seconds,
        "elapsed_seconds": round(elapsed, 2),
        "wav": str(wav_path),
        "wav_seconds": round(consumer.duration, 3),
        "wav_bytes": wav_path.stat().st_size if wav_path.exists() else 0,
        "wall_yield": round(consumer.duration / elapsed, 4) if elapsed else 0.0,
        "stats": stats.as_dict(),
        "daemon_state_final": gate.state,
        "motor_control_mode_final": gate.motor_mode,
        "process_cpu_seconds": round(cpu_seconds, 3),
        "process_cpu_percent_of_wall": round(
            100 * cpu_seconds / elapsed if elapsed else 0.0, 2
        ),
        "max_rss_kb": usage_after.ru_maxrss,
    }
    stats_path.write_text(json.dumps(summary, indent=2))
    log.add("capture_end", **{
        "wav_seconds": summary["wav_seconds"],
        "wall_yield": summary["wall_yield"],
        "yield_connected": summary["stats"]["yield_ratio"],
        "sessions": summary["stats"]["sessions"],
        "reconnects": summary["stats"]["reconnects"],
        "gaps": len(summary["stats"]["gaps"]),
        "cpu_percent": summary["process_cpu_percent_of_wall"],
    })
    log.close()
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
