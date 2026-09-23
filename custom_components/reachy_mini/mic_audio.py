"""Gateway-side helpers for the microphone tap.

These deliberately hold no robot, HA or WebRTC knowledge: the mic
consumer (:mod:`.mic_stream`) hands them plain 16 kHz mono s16 PCM and
they do the two things P1 needs — keep a rolling window of the newest
audio (the P2 detector reads it on trigger) and write a WAV file
(listening proof / tuning material).

Keep every consumer here non-blocking-ish: consumers run inline on the
receive path, so a slow one costs frames (see
:meth:`MicStreamClient.add_consumer`).
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

from .const import MIC_CHANNELS, MIC_SAMPLE_RATE, MIC_SAMPLE_WIDTH

_LOGGER = logging.getLogger(__name__)


class AudioRingBuffer:
    """Byte FIFO of the newest PCM, dropping the oldest on overrun.

    A detector that is handed "the last 1.5 s" must never miss *recent*
    audio because a reader stalled, so overflow drops from the front and
    is counted: ``dropped_bytes`` staying at zero proves the reader kept
    up.
    """

    def __init__(self, capacity_bytes: int) -> None:
        """Create a buffer holding at most ``capacity_bytes`` of PCM."""
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        self._capacity = capacity_bytes
        self._buffer = bytearray()
        self._dropped_bytes = 0

    @property
    def capacity_bytes(self) -> int:
        """Hard cap on retained PCM."""
        return self._capacity

    @property
    def available_bytes(self) -> int:
        """Bytes currently retained."""
        return len(self._buffer)

    @property
    def dropped_bytes(self) -> int:
        """Bytes lost to overruns since construction."""
        return self._dropped_bytes

    @property
    def seconds_available(self) -> float:
        """Retained audio expressed in seconds of the configured rate."""
        frame_bytes = MIC_SAMPLE_RATE * MIC_SAMPLE_WIDTH * MIC_CHANNELS
        return len(self._buffer) / frame_bytes

    def write(self, pcm: bytes) -> int:
        """Append PCM, dropping the oldest bytes if it does not fit.

        Returns the number of bytes dropped (0 when there was room).
        """
        if not pcm:
            return 0
        self._buffer.extend(pcm)
        overflow = len(self._buffer) - self._capacity
        if overflow <= 0:
            return 0
        del self._buffer[:overflow]
        self._dropped_bytes += overflow
        return overflow

    def read(self, n_bytes: int | None = None) -> bytes:
        """Pop the oldest ``n_bytes`` (or everything) out of the buffer."""
        if n_bytes is None or n_bytes >= len(self._buffer):
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        if n_bytes <= 0:
            return b""
        data = bytes(self._buffer[:n_bytes])
        del self._buffer[:n_bytes]
        return data

    def tail(self, n_bytes: int | None = None) -> bytes:
        """Return the newest ``n_bytes`` without consuming them."""
        if n_bytes is None or n_bytes >= len(self._buffer):
            return bytes(self._buffer)
        if n_bytes <= 0:
            return b""
        return bytes(self._buffer[-n_bytes:])

    def clear(self) -> None:
        """Drop every retained byte (does not reset the drop counter)."""
        self._buffer.clear()


class WavWriter:
    """Incremental 16 kHz mono s16 WAV writer.

    The stdlib ``wave`` module keeps the header honest on close, so a
    capture that is killed mid-way still yields a readable file if the
    writer is closed — the harness closes it in a ``finally``.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        sample_rate: int = MIC_SAMPLE_RATE,
        channels: int = MIC_CHANNELS,
        sample_width: int = MIC_SAMPLE_WIDTH,
    ) -> None:
        """Bind the writer to ``path`` (created on :meth:`open`)."""
        self._path = Path(path)
        self._sample_rate = sample_rate
        self._channels = channels
        self._sample_width = sample_width
        self._frames_written = 0
        self._wave: wave.Wave_write | None = None

    @property
    def path(self) -> Path:
        """Filesystem path written to."""
        return self._path

    @property
    def frame_bytes(self) -> int:
        """Bytes per PCM frame (all channels of one sample instant)."""
        return self._sample_width * self._channels

    @property
    def frames_written(self) -> int:
        """PCM frames written so far (one frame = one sample instant)."""
        return self._frames_written

    @property
    def bytes_written(self) -> int:
        """PCM payload bytes written so far (header excluded)."""
        return self._frames_written * self.frame_bytes

    @property
    def duration(self) -> float:
        """Seconds of audio written so far."""
        return self._frames_written / self._sample_rate

    def open(self) -> None:
        """Create the file and write the WAV header."""
        if self._wave is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = wave.open(str(self._path), "wb")
        handle.setnchannels(self._channels)
        handle.setsampwidth(self._sample_width)
        handle.setframerate(self._sample_rate)
        self._wave = handle

    def write(self, pcm: bytes) -> None:
        """Append PCM. Lengths that are not whole frames are rejected."""
        if not pcm:
            return
        if len(pcm) % self.frame_bytes:
            raise ValueError(
                f"PCM length {len(pcm)} is not a multiple of {self.frame_bytes}"
            )
        if self._wave is None:
            self.open()
        assert self._wave is not None
        self._wave.writeframesraw(pcm)
        self._frames_written += len(pcm) // self.frame_bytes

    def close(self) -> None:
        """Finalise the WAV header (idempotent)."""
        if self._wave is None:
            return
        handle, self._wave = self._wave, None
        handle.close()

    def __enter__(self) -> WavWriter:
        """Context-manager sugar so a crash still closes the file."""
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close on the way out, exceptions or not."""
        self.close()


class MicDumpConsumer:
    """The P1 consumer: rolling window plus an on-disk WAV.

    Small enough to be the default sink for a live capture, and used by
    the tests as the shape of "a consumer that keeps its own state".
    """

    def __init__(
        self,
        path: str | Path,
        *,
        ring_seconds: float = 5.0,
        sample_rate: int = MIC_SAMPLE_RATE,
    ) -> None:
        """Create the WAV writer and a ``ring_seconds`` ring buffer."""
        self.writer = WavWriter(path, sample_rate=sample_rate)
        frame_bytes = sample_rate * MIC_SAMPLE_WIDTH * MIC_CHANNELS
        self.ring = AudioRingBuffer(int(frame_bytes * ring_seconds))
        self.frames = 0

    async def __call__(self, pcm: bytes) -> None:
        """Consumer interface: one decoded PCM chunk per audio frame."""
        self.writer.write(pcm)
        self.ring.write(pcm)
        self.frames += 1

    @property
    def duration(self) -> float:
        """Seconds of audio captured so far."""
        return self.writer.duration

    def close(self) -> None:
        """Finalise the WAV file."""
        self.writer.close()
