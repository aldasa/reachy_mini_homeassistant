"""Audio fetch + transcode helpers for ``reachy_mini.play_audio``.

Route A′ (REST upload + ``play_sound``) needs the audio as a *file* the
daemon can decode. This module turns whatever the caller supplied into
one:

1. :func:`read_media` resolves the reference — an ``http(s)`` URL, a
   ``/media/...`` (or other HA-relative) path, a ``media-source://`` id
   — and returns the raw bytes, bounded by
   :data:`~.const.MAX_MEDIA_FETCH_BYTES`.
2. :func:`to_wav_s16` decodes that with PyAV and re-encodes it as PCM
   s16 WAV at 16 kHz mono (:data:`~.const.WAV_SAMPLE_RATE`), the format
   the daemon's upload route accepts and every GStreamer build can play
   back.

Why PyAV and never a subprocess: Home Assistant OS gives no guarantee
of an ``ffmpeg`` *binary*, while ``av`` is already a hard dependency of
the camera's aiortc stack — so decoding happens in-process with no
shell-out (DESIGN.md §6). Decoding is CPU-bound, so the service runs
:func:`to_wav_s16` through ``hass.async_add_executor_job`` rather than
on the event loop.

Everything here raises :class:`AudioSourceError` (a
:class:`~homeassistant.exceptions.ServiceValidationError`) for problems
the caller can act on: an unsupported scheme, a failed fetch, a payload
over the cap, or bytes PyAV cannot decode.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

import aiohttp
import av
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ALLOWED_SOUND_EXTENSIONS,
    MAX_MEDIA_FETCH_BYTES,
    MAX_SOUND_UPLOAD_BYTES,
    MEDIA_FETCH_TIMEOUT,
    WAV_CHANNELS,
    WAV_SAMPLE_RATE,
)

_LOGGER = logging.getLogger(__name__)

# Schemes we will fetch over the network. Deliberately no file:// — HA
# media references are resolved through hass.config paths instead, and
# an arbitrary file:// URL from an automation would be a needless
# local-file-read primitive.
FETCHABLE_SCHEMES: tuple[str, ...] = ("http", "https")

# Media-source ids and HA-relative web paths.
MEDIA_SOURCE_SCHEME = "media-source"

_READ_CHUNK = 1 << 16


class AudioSourceError(ServiceValidationError):
    """The supplied ``media`` could not be fetched, sized or decoded.

    Subclasses :class:`ServiceValidationError` so HA surfaces the
    message to the caller as a validation failure rather than an
    internal error.
    """


def source_extension(media: str) -> str | None:
    """Return the allow-listed extension of a media reference.

    Used to classify a source (``.mp3`` TTS cache file, ``.wav``
    sample, …) for logging and for the filename we derive from it. A
    reference with no extension, or one outside the daemon's
    allow-list, returns ``None`` — that is *not* an error, because the
    source is transcoded to WAV regardless; only the name we upload has
    to be allow-listed.
    """
    path = urlsplit(media).path or media
    suffix = PurePosixPath(unquote(path)).suffix.lower()
    return suffix if suffix in ALLOWED_SOUND_EXTENSIONS else None


def upload_filename(data: bytes, extension: str = ".wav") -> str:
    """Build the daemon-side filename for an upload.

    Content-addressed (``ha_<sha256[:8]>.wav``), so replaying the same
    asset overwrites its previous upload instead of filling the
    daemon's temp directory, and so a retry of a failed upload is
    idempotent daemon-side.

    Raises:
        AudioSourceError: if *extension* is not on the daemon's
            allow-list — the upload would be rejected with a 400.

    """
    if extension.lower() not in ALLOWED_SOUND_EXTENSIONS:
        raise AudioSourceError(
            f"'{extension}' is not an allowed upload extension; the daemon "
            f"accepts: {', '.join(sorted(ALLOWED_SOUND_EXTENSIONS))}"
        )
    digest = hashlib.sha256(data).hexdigest()[:8]
    return f"ha_{digest}{extension.lower()}"


def _size_str(num_bytes: int) -> str:
    """Human-readable size for error messages."""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.0f} MiB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f} KiB"
    return f"{num_bytes} bytes"


def _declared_length(resp: aiohttp.ClientResponse) -> int | None:
    """The response's declared ``Content-Length``, if it sent one.

    Checked before reading so an obviously oversized body is rejected
    without streaming it; the streamed byte count below remains the
    authoritative check, since chunked responses declare nothing.
    """
    raw = resp.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def ensure_within_upload_cap(
    data: bytes, cap: int | None = None
) -> None:
    """Reject an encoded payload the daemon's upload route would refuse.

    The daemon caps uploads at 25 MiB *before* its GStreamer probe, so
    sending more than that only ever produces a 400 on the far side.

    Args:
        data: Encoded audio about to be uploaded.
        cap: Override for the cap; defaults to
            :data:`~.const.MAX_SOUND_UPLOAD_BYTES` (resolved at call
            time so tests can lower it).

    """
    limit = MAX_SOUND_UPLOAD_BYTES if cap is None else cap
    if len(data) > limit:
        raise AudioSourceError(
            f"audio is {_size_str(len(data))} after encoding, over the "
            f"daemon's {_size_str(limit)} upload cap — trim the clip or use a "
            "shorter TTS reply"
        )


def _resampled_frames(resampler: av.AudioResampler, frame: av.AudioFrame | None):
    """Normalise ``AudioResampler.resample()`` across PyAV versions.

    Newer PyAV returns a list of frames (and accepts ``None`` to flush);
    older releases returned a single frame or ``None``.
    """
    result = resampler.resample(frame) if frame is not None else resampler.resample(None)
    if result is None:
        return []
    return result if isinstance(result, list) else [result]


def to_wav_s16(
    data: bytes,
    rate: int = WAV_SAMPLE_RATE,
    channels: int = WAV_CHANNELS,
) -> bytes:
    """Decode *data* (any PyAV-readable container) to in-memory PCM s16 WAV.

    Runs in an executor from the service — it blocks while FFmpeg
    demuxes and decodes.

    Args:
        data: Source audio bytes (WAV, MP3, OGG/Opus, FLAC, M4A, …).
        rate: Output sample rate in Hz.
        channels: Output channel count (1 = mono).

    Returns:
        A complete WAV file as bytes.

    Raises:
        AudioSourceError: if the payload has no audio stream or cannot
            be decoded.

    """
    layout = "mono" if channels == 1 else "stereo"
    out = io.BytesIO()
    try:
        with av.open(io.BytesIO(data)) as container:
            if not container.streams.audio:
                raise AudioSourceError(
                    "the supplied media has no audio stream to play"
                )
            in_stream = container.streams.audio[0]
            resampler = av.AudioResampler(
                format="s16", layout=layout, rate=rate
            )
            with av.open(out, mode="w", format="wav") as out_container:
                out_stream = out_container.add_stream("pcm_s16le", rate=rate)
                out_stream.layout = layout
                for frame in container.decode(in_stream):
                    for resampled in _resampled_frames(resampler, frame):
                        for packet in out_stream.encode(resampled):
                            out_container.mux(packet)
                # Flush the resampler, then the encoder — otherwise the
                # tail of the clip is silently dropped.
                for resampled in _resampled_frames(resampler, None):
                    for packet in out_stream.encode(resampled):
                        out_container.mux(packet)
                for packet in out_stream.encode(None):
                    out_container.mux(packet)
    except AudioSourceError:
        raise
    except av.FFmpegError as err:
        raise AudioSourceError(f"could not decode the supplied media: {err}") from err

    encoded = out.getvalue()
    if not encoded:
        raise AudioSourceError("the supplied media decoded to zero audio frames")
    return encoded


async def fetch_bytes(
    hass: HomeAssistant,
    url: str,
    *,
    max_bytes: int | None = None,
    timeout: float | None = None,
) -> bytes:
    """GET *url* and return its body, refusing anything over the cap.

    Bounded twice: the declared ``Content-Length`` is checked before
    reading, and the stream is counted as it arrives (a chunked
    response has no declared length to trust).

    Args:
        max_bytes: Fetch cap; defaults to
            :data:`~.const.MAX_MEDIA_FETCH_BYTES` (resolved at call time
            so tests can lower it).
        timeout: Total fetch timeout; defaults to
            :data:`~.const.MEDIA_FETCH_TIMEOUT`.

    Raises:
        AudioSourceError: on a non-200 response, a transport error, a
            timeout, or a body over the cap.

    """
    limit = MAX_MEDIA_FETCH_BYTES if max_bytes is None else max_bytes
    total_timeout = MEDIA_FETCH_TIMEOUT if timeout is None else timeout

    if urlsplit(url).scheme not in FETCHABLE_SCHEMES:
        raise AudioSourceError(
            f"unsupported media URL scheme in '{url}' — only "
            f"{' and '.join(FETCHABLE_SCHEMES)} are allowed"
        )

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=total_timeout)
        ) as resp:
            if resp.status != 200:
                raise AudioSourceError(
                    f"could not fetch media from {url}: HTTP {resp.status}"
                )
            declared = _declared_length(resp)
            if declared is not None and declared > limit:
                raise AudioSourceError(
                    f"media at {url} is {_size_str(declared)}, over the "
                    f"{_size_str(limit)} fetch cap"
                )
            body = bytearray()
            async for chunk in resp.content.iter_chunked(_READ_CHUNK):
                body.extend(chunk)
                if len(body) > limit:
                    raise AudioSourceError(
                        f"media at {url} exceeded the {_size_str(limit)} "
                        "fetch cap"
                    )
    except AudioSourceError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise AudioSourceError(f"could not fetch media from {url}: {err}") from err

    if not body:
        raise AudioSourceError(f"media at {url} was empty")
    return bytes(body)


def _resolve_local_path(hass: HomeAssistant, path: str) -> str:
    """Map an HA-relative media path to a filesystem path.

    Three shapes are understood:

    * ``/media/<name>/rest`` — a configured media directory (HA's
      default is ``local`` → ``<config>/media``), so the first segment
      is a *directory name*, not a filesystem one.
    * ``/media/rest`` — the media directory itself.
    * ``/local/rest`` — ``<config>/www`` (HA's ``/local`` static route).

    Anything else under ``/`` is treated as config-dir-relative, and a
    plain relative value is resolved against the config dir. Nothing is
    read from outside those roots: a reference can only name a file HA
    itself would serve.
    """
    if not path.startswith("/") or path.startswith("//"):
        return hass.config.path(path)

    relative = path.lstrip("/")
    if relative.startswith("local/"):
        return hass.config.path("www", relative[len("local/") :])

    if relative.startswith("media/"):
        rest = relative[len("media/") :]
        for name, base in (getattr(hass.config, "media_dirs", None) or {}).items():
            prefix = f"{name}/"
            if rest.startswith(prefix):
                return os.path.join(base, rest[len(prefix) :])
        return hass.config.path("media", rest)

    return hass.config.path(relative)


async def _read_local(hass: HomeAssistant, path: str) -> bytes:
    """Read a local file off the event loop."""

    def _read() -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    try:
        return await hass.async_add_executor_job(_read)
    except FileNotFoundError as err:
        raise AudioSourceError(f"no such local media file: {path}") from err
    except OSError as err:
        raise AudioSourceError(f"could not read {path}: {err}") from err


def _absolute_ha_url(hass: HomeAssistant, path: str) -> str | None:
    """Build an absolute URL for a path served by this HA instance.

    Used as a fallback when a reference names something HA serves over
    HTTP rather than a file on disk — ``/api/tts_proxy/<hash>.mp3`` from
    a ``media-source://tts/...`` resolution, for instance.
    """
    if not path.startswith("/") or path.startswith("//"):
        return None
    for base in (hass.config.internal_url, hass.config.external_url):
        if base:
            return f"{base.rstrip('/')}{path}"
    return None


async def read_media(hass: HomeAssistant, media: str) -> bytes:
    """Resolve a ``play_audio`` ``media`` reference to raw bytes.

    Accepts, in order:

    * a ``media-source://`` id (resolved through HA's media_source,
      then fetched/read as its target),
    * an ``http(s)`` URL — a TTS cache file, for example,
    * a path — ``/media/ding.wav``, ``/local/ding.wav``, an absolute
      path on the HA host, or a path relative to the config dir. A path
      that is not a readable local file is retried against HA's own
      internal URL, which is how TTS proxy URLs resolve.

    Raises:
        AudioSourceError: for an empty reference, an unsupported
            scheme, a failed fetch or read, or an oversized payload.

    """
    reference = (media or "").strip()
    if not reference:
        raise AudioSourceError("'media' must not be empty")

    scheme = urlsplit(reference).scheme

    if scheme == MEDIA_SOURCE_SCHEME:
        reference = await _resolve_media_source(hass, reference)
        scheme = urlsplit(reference).scheme

    if scheme in FETCHABLE_SCHEMES:
        return await fetch_bytes(hass, reference)
    if scheme:
        raise AudioSourceError(
            f"unsupported media reference '{media}' — use an http(s) URL, a "
            "/media path, or a media-source:// id"
        )

    try:
        return await _read_local(hass, _resolve_local_path(hass, reference))
    except AudioSourceError as err:
        absolute = _absolute_ha_url(hass, reference)
        if absolute is None:
            raise
        _LOGGER.debug(
            "%s is not a local file; fetching it from HA: %s", reference, err
        )
        return await fetch_bytes(hass, absolute)


async def _resolve_media_source(hass: HomeAssistant, media_id: str) -> str:
    """Resolve a ``media-source://`` id to a fetchable URL or path."""
    from homeassistant.components import media_source

    try:
        resolved = await media_source.async_resolve_media(hass, media_id, None)
    except Exception as err:  # media_source raises several unrelated types
        raise AudioSourceError(
            f"could not resolve media source '{media_id}': {err}"
        ) from err
    url = getattr(resolved, "url", None)
    if not url:
        raise AudioSourceError(f"media source '{media_id}' resolved to no URL")
    return url
