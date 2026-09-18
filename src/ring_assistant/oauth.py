"""Ring OAuth token exchange — the S2 machine that mints RING_API_TOKEN.

Pinned to the documented one-way account-linking flow
(https://developer.amazon.com/docs/ring/api-documentation.html §5):

* Ring's backend POSTs a one-time authorization code (60 s lifetime)
  to the partner's Token Exchange URL, backend-to-backend. There is NO
  partner-hosted authorize URL and no PKCE: this is the confidential-
  client flow — the client secret authenticates the app, and Ring's
  OAuth server must be called server-to-server (browser-originated
  requests are CORS-blocked by design).
* The partner exchanges the code within its 60 s lifetime::

      POST https://oauth.ring.com/oauth/token
      Content-Type: application/x-www-form-urlencoded
      grant_type=authorization_code&client_id&code&client_secret

* The response carries ``access_token`` (~4 h Bearer), ``refresh_token``
  (~30 d), ``scope``, ``expires_in`` (documented 14400) and
  ``token_type``. Refreshing uses the same endpoint with
  ``grant_type=refresh_token``.

HTTP goes through the same injected transport as ``events_api`` /
``llm`` — tests and the offline mock run in-process, zero sockets.
Tokens are values to persist, never to log or echo back: no exception
message and no route response in this module's callers includes them.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlencode

from .events_api import HttpTransport, HttpResponse, _urllib_transport

DOCUMENTED_TOKEN_URL = "https://oauth.ring.com/oauth/token"
AUTH_CODE_LIFETIME_S = 60  # documented: exchange within 60 s, one-time use
DOCUMENTED_EXPIRES_IN = 14400  # documented ~4 h access-token lifetime
DOCUMENTED_TOKEN_TYPE = "Bearer"


class OAuthError(RuntimeError):
    """A token-exchange call failed (config, transport, HTTP, or payload)."""

    def __init__(self, message: str, *, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class OAuthConfigError(OAuthError):
    """Client credentials (or the code itself) are missing — caller-side."""


@dataclass(frozen=True)
class TokenBundle:
    """One successful token response; ``obtained_at`` is epoch seconds."""

    access_token: str
    refresh_token: str
    scope: str
    expires_in: int
    token_type: str
    obtained_at: float

    @property
    def expires_at(self) -> float:
        return self.obtained_at + self.expires_in

    def expired(self, now: float, *, skew_s: float = 30.0) -> bool:
        """True past ``expires_in`` minus a refresh skew."""
        return now >= self.expires_at - skew_s


@dataclass
class TokenExchanger:
    """Client for the two documented grants of ``oauth.ring.com``.

    Fail-closed like the rest of the pipeline: empty credentials are a
    construction error, never a silent anonymous request.
    """

    client_id: str
    client_secret: str
    token_url: str = DOCUMENTED_TOKEN_URL
    timeout_s: float = 10.0
    transport: HttpTransport = _urllib_transport

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret:
            raise OAuthConfigError(
                "RING_CLIENT_ID / RING_CLIENT_SECRET are not set; refusing a "
                "token exchange with no client credentials"
            )

    # -- documented grants ---------------------------------------------------

    def exchange_code(self, code: str, *, now: float | None = None) -> TokenBundle:
        """``grant_type=authorization_code`` — the registration-day grant."""
        if not code:
            raise OAuthConfigError("authorization code is empty")
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "code": code,
                "client_secret": self.client_secret,
            },
            now=now,
        )

    def refresh(self, refresh_token: str, *, now: float | None = None) -> TokenBundle:
        """``grant_type=refresh_token`` — rotate before the ~4 h expiry."""
        if not refresh_token:
            raise OAuthConfigError("refresh token is empty")
        return self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            now=now,
        )

    # -- plumbing ------------------------------------------------------------

    def _token_request(self, form: Mapping[str, str], *, now: float | None) -> TokenBundle:
        # Credentials ride in the form BODY per the documented request —
        # never in the URL, never in a Basic-auth header.
        body = urlencode(dict(form)).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        try:
            response = self.transport("POST", self.token_url, body, headers, self.timeout_s)
        except urllib.error.URLError as exc:
            raise OAuthError(f"token endpoint transport failure: {exc}") from exc
        if response.status != 200:
            raise _error_from_response(response)
        return _bundle_from_response(response, now=now)


def _error_from_response(response: HttpResponse) -> OAuthError:
    """Surface status + JSON:API error body — never the request body."""
    status = response.status
    code, detail = "", f"token endpoint returned HTTP {status}"
    try:
        errors = json.loads(response.body.decode("utf-8")).get("errors")
        first = errors[0] if isinstance(errors, list) and errors else None
        if isinstance(first, dict):
            code = str(first.get("code", ""))
            detail = f"token endpoint returned HTTP {status} {code}"
            if first.get("detail"):
                detail += f" — {first['detail']}"
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        pass  # non-JSON error body: keep the plain status line
    return OAuthError(detail, status=status, code=code or "TOKEN_ENDPOINT_ERROR")


def _bundle_from_response(response: HttpResponse, *, now: float | None) -> TokenBundle:
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthError(f"token endpoint returned non-JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise OAuthError("token endpoint returned a non-object JSON body")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError("token response missing access_token")
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int):
        # documented 14400; tolerate omission, refuse garbage
        expires_in = DOCUMENTED_EXPIRES_IN
    return TokenBundle(
        access_token=access_token,
        refresh_token=str(payload.get("refresh_token", "") or ""),
        scope=str(payload.get("scope", "") or ""),
        expires_in=expires_in,
        token_type=str(payload.get("token_type", "") or DOCUMENTED_TOKEN_TYPE),
        obtained_at=time.time() if now is None else now,
    )


@dataclass
class TokenStore:
    """Persist the newest bundle as JSON with owner-only permissions.

    This is the mint target: with a store configured, a successful
    exchange (or refresh) lands here, and the value is pasted into
    ``.env`` as ``RING_API_TOKEN`` (or composed directly) — never
    printed, never echoed, never committed. ``.gitignore`` it with the
    rest of the secrets' homes.
    """

    path: Path

    def save(self, bundle: TokenBundle) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "access_token": bundle.access_token,
            "refresh_token": bundle.refresh_token,
            "scope": bundle.scope,
            "expires_in": bundle.expires_in,
            "token_type": bundle.token_type,
            "obtained_at": bundle.obtained_at,
            "expires_at": bundle.expires_at,
        }
        # write-then-rename so a crash never leaves a half-written store
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def load(self) -> TokenBundle | None:
        """Newest bundle, or None when absent/corrupt (reads never raise)."""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
            return None
        try:
            return TokenBundle(
                access_token=payload["access_token"],
                refresh_token=str(payload.get("refresh_token", "") or ""),
                scope=str(payload.get("scope", "") or ""),
                expires_in=int(payload.get("expires_in", DOCUMENTED_EXPIRES_IN)),
                token_type=str(payload.get("token_type", "") or DOCUMENTED_TOKEN_TYPE),
                obtained_at=float(payload["obtained_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
