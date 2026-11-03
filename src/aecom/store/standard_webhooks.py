"""Standard Webhooks signature check (https://www.standardwebhooks.com), shared by the webhook routes.

Headers ``webhook-id`` / ``webhook-timestamp`` / ``webhook-signature``. The signature is any ``v1,<base64>`` entry of
the space-separated header, equal to the base64 HMAC-SHA256 of ``{id}.{timestamp}.{raw body}``. The timestamp must be
within ``TOLERANCE_S`` of now (replay protection).

Providers disagree on how the secret becomes the HMAC key, so both forms are tried: the whole secret string as UTF-8
(Polar secrets created before 2026-09-08) and the base64
part after a ``whsec_`` prefix (the Standard Webhooks spec). Stdlib only, no SDK.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time

TOLERANCE_S = 5 * 60


def keys(secret: str) -> list[bytes]:
    out = [secret.encode()]
    for prefix in ("whsec_",):
        if secret.startswith(prefix):
            try:
                out.append(base64.b64decode(secret.removeprefix(prefix), validate=True))
            except (ValueError, binascii.Error):
                pass
    return out


def verify(raw_body: bytes, msg_id: str | None, timestamp: str | None, signature: str | None, secret: str | None,
           now: float | None = None) -> bool:
    """True if the signature matches under one of the key forms and the timestamp is fresh. Missing parts -> False."""
    if not (secret and msg_id and timestamp and signature):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - ts) > TOLERANCE_S:
        return False
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + raw_body
    ok = False
    for key in keys(secret):
        expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest())
        for entry in signature.split():
            version, _, sig = entry.partition(",")
            if version == "v1" and hmac.compare_digest(expected, sig.encode()):
                ok = True  # no early exit: compare every entry
    return ok
