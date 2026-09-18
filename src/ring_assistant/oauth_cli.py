"""``mint-token`` CLI: run the S2 token exchange by hand.

The server normally performs the exchange the moment Ring POSTs a code
to the Token Exchange URL. This CLI covers the two manual legs of the
same machine (docs/REGISTRATION-GUIDE.md §6):

    mint-token <code>     exchange a fresh authorization code (60 s
                          lifetime — paste it the moment you have it)
    mint-token --refresh  rotate the stored bundle (access tokens live
                          ~4 h)

The bundle persists to Turso when RING_TURSO_URL + RING_TURSO_TOKEN are
set (the deployed store), else to the RING_TOKEN_STORE file. The printed
summary carries NO token material — copy the access token from the store
into ``.env`` as ``RING_API_TOKEN`` (never chat, never commit).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .oauth import OAuthConfigError, OAuthError, TokenExchanger, TokenStore
from .settings import load_env_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mint-token",
        description="Exchange a Ring authorization code (or refresh the stored "
        "bundle) for the OAuth tokens that mint RING_API_TOKEN.",
    )
    parser.add_argument("code", nargs="?", help="authorization code from Ring (60 s lifetime)")
    parser.add_argument(
        "--refresh", action="store_true", help="refresh the bundle in RING_TOKEN_STORE"
    )
    args = parser.parse_args(argv)

    load_env_file()  # best-effort .env; real environment always wins
    import os

    client_id = os.environ.get("RING_CLIENT_ID", "")
    client_secret = os.environ.get("RING_CLIENT_SECRET", "")
    store_path = os.environ.get("RING_TOKEN_STORE", "")
    turso_url = os.environ.get("RING_TURSO_URL", "")
    turso_token = os.environ.get("RING_TURSO_TOKEN", "")

    if not args.code and not args.refresh:
        parser.error("pass an authorization code or --refresh")
    # Deployed storage: Turso wins when BOTH coordinates exist (the bundle
    # must survive the deploy's ephemeral disk); half-set is an error;
    # neither set falls back to the RING_TOKEN_STORE file.
    store = None
    store_label = ""
    if turso_url or turso_token:
        if not (turso_url and turso_token):
            print(
                "error: RING_TURSO_URL and RING_TURSO_TOKEN must be set together",
                file=sys.stderr,
            )
            return 1
        from .turso import TursoTokenStore, connect_turso

        store = TursoTokenStore(connect_turso(turso_url, turso_token))
        store_label = "Turso (tokens table)"
    elif store_path:
        store = TokenStore(Path(store_path))
        store_label = store_path
    if store is None:
        print(
            "RING_TOKEN_STORE is not set; the bundle will NOT persist. "
            "Set it in .env so the minted token survives this command.",
            file=sys.stderr,
        )
    try:
        exchanger = TokenExchanger(client_id=client_id, client_secret=client_secret)
    except OAuthConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.refresh:
            bundle = store.load() if store is not None else None
            if bundle is None or not bundle.refresh_token:
                print(
                    "error: no bundle with a refresh token in the token store; "
                    "exchange a code first",
                    file=sys.stderr,
                )
                return 1
            bundle = exchanger.refresh(bundle.refresh_token)
        else:
            bundle = exchanger.exchange_code(args.code)
    except OAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if store is not None:
        store.save(bundle)
    expires_at = datetime.fromtimestamp(bundle.expires_at, tz=timezone.utc)
    print(
        f"token exchanged: {bundle.token_type}, expires_in={bundle.expires_in}s "
        f"(at {expires_at.isoformat()})"
    )
    if store is not None:
        print(f"persisted: {store_label} (access token ready to become RING_API_TOKEN)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
