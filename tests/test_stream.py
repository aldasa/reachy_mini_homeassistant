"""Stream client: DTLS interop, signalling protocol, frame pipeline."""

from __future__ import annotations


def test_interop_certificate_offers_both_cipher_families() -> None:
    """The robot's Linux gst build has an RSA DTLS cert; macOS ECDSA.

    aiortc's default list is ECDSA-only, which the robot answers with a
    fatal handshake_failure alert — both families must be offered.
    """
    from aiortc.rtcdtlstransport import SRTP_PROFILES
    from OpenSSL import SSL

    from custom_components.reachy_mini.stream import InteropCertificate

    cert = InteropCertificate.generate()
    ctx = cert._create_ssl_context(SRTP_PROFILES)
    ciphers = SSL.Connection(ctx).get_cipher_list()
    assert "ECDHE-RSA-AES128-GCM-SHA256" in ciphers
    assert "ECDHE-ECDSA-AES128-GCM-SHA256" in ciphers
