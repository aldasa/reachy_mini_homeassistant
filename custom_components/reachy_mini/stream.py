"""Shared WebRTC consumer session for the Reachy Mini camera.

The robot streams camera + audio through GStreamer ``webrtcsink`` with
its built-in gst-webrtc-signalling server on ``ws://<host>:8443``. That
producer only supports the producer-offers flow (consumer-initiated
offers are answered with an immediate ``endSession``), and HA's frontend
can only be a WebRTC *offerer* — so this module terminates the robot's
WebRTC media on the HA host with aiortc and hands decoded frames to the
camera entity.

Interop notes (verified against a live robot, daemon 1.8.4 / gst 1.28.3):

- The robot's Linux GStreamer build has an RSA DTLS certificate while
  aiortc's default cipher list is ECDSA-only; RSA ECDHE suites must be
  offered or the handshake dies with a fatal alert 40 — see
  :class:`InteropCertificate`.
- gst ``webrtcbin`` ignores ``a=candidate`` lines embedded in the answer
  SDP; local ICE candidates must be trickled as individual ``ice``
  signalling messages.
"""

from __future__ import annotations

from aiortc.rtcdtlstransport import RTCCertificate

from .const import DTLS_CIPHER_LIST


class InteropCertificate(RTCCertificate):
    """RTCCertificate whose DTLS context offers RSA ECDHE suites too."""

    def _create_ssl_context(self, srtp_profiles):  # type: ignore[no-untyped-def]
        ctx = super()._create_ssl_context(srtp_profiles)
        ctx.set_cipher_list(DTLS_CIPHER_LIST)
        return ctx

    @classmethod
    def generate(cls) -> InteropCertificate:
        """Self-signed certificate, like aiortc's default one."""
        base = RTCCertificate.generateCertificate()
        return cls(key=base._key, cert=base._cert)
