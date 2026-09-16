"""HMAC-SHA256 webhook signature verification.

Contract: the sender signs the RAW request body with the shared secret
and sends the hex digest in the signature header as::

    sha256=<hexdigest>

This mirrors the ``sha256=`` HMAC scheme Ring's webhooks document. The
HEADER NAME itself is configuration (``RING_SIGNATURE_HEADER``, default
``x-ring-signature``) because the offline skeleton does not pin the
live header name — see VALIDATION.md caveats.

Properties:
- Constant-time comparison (``hmac.compare_digest``) — no early-exit
  digest oracle.
- An empty secret is a misconfiguration, not a mode: signing AND
  verifying with an empty key is refused.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_SCHEME = "sha256"


class SignatureError(Exception):
    """The webhook could not be authenticated."""


def sign_payload(body: bytes, secret: str) -> str:
    """Compute the signature header value for ``body`` (tests/synthetic sender)."""
    if not secret:
        raise SignatureError("empty webhook secret — refusing to sign with no key")
    if not isinstance(body, bytes):
        raise TypeError(f"body must be bytes, got {type(body).__name__}")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_SCHEME}={digest}"


def verify_signature(body: bytes, header_value: str | None, secret: str) -> None:
    """Authenticate ``body`` against the signature header value.

    Raises SignatureError (never a bare bool) so callers cannot
    accidentally ignore a failed check: an unauthenticated event must
    never enter the pipeline.
    """
    if not secret:
        raise SignatureError(
            "empty webhook secret — configure RING_WEBHOOK_SECRET (see .env.example)"
        )
    if header_value is None or not str(header_value).strip():
        raise SignatureError("missing signature header")
    scheme, sep, digest = str(header_value).strip().partition("=")
    if not sep or scheme.strip().lower() != SIGNATURE_SCHEME:
        raise SignatureError(
            f"unsupported signature format {header_value.strip()!r} — expected '{SIGNATURE_SCHEME}=<hexdigest>'"
        )
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = digest.strip().lower()
    if len(provided) != len(expected) or not hmac.compare_digest(expected, provided):
        raise SignatureError("signature mismatch")
