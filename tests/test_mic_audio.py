"""Mic audio utils: ring buffer, WAV writer, dump consumer."""

from __future__ import annotations

import wave

import pytest

from custom_components.reachy_mini.const import MIC_SAMPLE_RATE
from custom_components.reachy_mini.mic_audio import (
    AudioRingBuffer,
    MicDumpConsumer,
    WavWriter,
)

FRAME_BYTES = 320 * 2  # 20 ms of 16 kHz mono s16


def pcm(frames: int, start: int = 0) -> bytes:
    """Deterministic PCM: one int16 sample per value in range."""
    return b"".join(
        int(v).to_bytes(2, "little", signed=True)
        for v in range(start, start + frames)
    )


def test_ring_buffer_drops_oldest_and_counts_loss() -> None:
    ring = AudioRingBuffer(capacity_bytes=FRAME_BYTES * 2)
    ring.write(pcm(320, 0))
    ring.write(pcm(320, 1000))
    dropped = ring.write(pcm(320, 2000))
    # Third write overruns: the oldest frame is gone, the newest kept.
    assert dropped == FRAME_BYTES
    assert ring.dropped_bytes == FRAME_BYTES
    assert ring.available_bytes == FRAME_BYTES * 2
    assert ring.tail(FRAME_BYTES) == pcm(320, 2000)
    assert ring.available_bytes == FRAME_BYTES * 2  # tail() does not consume


def test_ring_buffer_read_pops_oldest_first() -> None:
    ring = AudioRingBuffer(capacity_bytes=FRAME_BYTES * 4)
    ring.write(pcm(320, 0))
    ring.write(pcm(320, 1000))
    assert ring.read(FRAME_BYTES) == pcm(320, 0)
    assert ring.read() == pcm(320, 1000)
    assert ring.available_bytes == 0


def test_ring_buffer_seconds_available_uses_pcm_rate() -> None:
    ring = AudioRingBuffer(capacity_bytes=MIC_SAMPLE_RATE * 2 * 5)
    ring.write(pcm(320))
    assert ring.seconds_available == pytest.approx(0.02, abs=1e-6)


def test_ring_buffer_rejects_empty_capacity() -> None:
    with pytest.raises(ValueError):
        AudioRingBuffer(capacity_bytes=0)


def test_wav_writer_round_trips_pcm(tmp_path) -> None:
    path = tmp_path / "tap.wav"
    writer = WavWriter(path)
    writer.write(pcm(320, 0))
    writer.write(pcm(320, 320))
    writer.close()

    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == MIC_SAMPLE_RATE
        assert handle.getnframes() == 640
        assert handle.readframes(640) == pcm(320, 0) + pcm(320, 320)
    assert writer.duration == pytest.approx(0.04)
    assert writer.frames_written == 640


def test_wav_writer_header_is_valid_before_close(tmp_path) -> None:
    """A capture killed mid-way must still leave a readable file."""
    path = tmp_path / "partial.wav"
    writer = WavWriter(path)
    writer.write(pcm(320))
    writer.close()
    writer.close()  # idempotent

    with wave.open(str(path), "rb") as handle:
        assert handle.getnframes() == 320


def test_wav_writer_rejects_partial_frames(tmp_path) -> None:
    writer = WavWriter(tmp_path / "bad.wav")
    with pytest.raises(ValueError):
        writer.write(b"\x00\x01\x02")


async def test_dump_consumer_writes_wav_and_ring(tmp_path) -> None:
    consumer = MicDumpConsumer(tmp_path / "mic.wav", ring_seconds=0.1)
    await consumer(pcm(320, 0))
    await consumer(pcm(320, 320))
    consumer.close()

    assert consumer.frames == 2
    assert consumer.duration == pytest.approx(0.04)
    assert consumer.ring.available_bytes <= consumer.ring.capacity_bytes
    # 0.1 s ring holds the newest three frames at most.
    assert consumer.ring.seconds_available <= 0.1 + 1e-9
    with wave.open(str(consumer.writer.path), "rb") as handle:
        assert handle.getnframes() == 640
