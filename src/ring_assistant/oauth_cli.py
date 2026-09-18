"""``mint-token`` CLI: run the S2 token exchange by hand.

The server normally performs the exchange the moment Ring POSTs a code
to the Token Exchange URL. This CLI covers the two manual legs of the
same machine (docs/REGISTRATION-GUIDE.md §6):

    mint-token <code>     exchange a fresh authorization code (60 s
                          lifetime — paste it the moment you have it)
    mint-token --refresh  rotate the bundle stored via RING_TOKEN_STORE
                          (access tokens live ~4 h)

Both persist to RING_TOKEN_STORE when set; the printed summary carries
NO token material — copy the access token from the store file into
``.env`` as ``RING_API_TOKEN`` (never chat, never commit).
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

    if not args.code and not args.refresh:
        parser.error("pass an authorization code or --refresh")
    if not store_path:
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
            store = TokenStore(Path(store_path)) if store_path else None
            bundle = store.load() if store else None
            if bundle is None or not bundle.refresh_token:
                print(
                    "error: no bundle with a refresh token in RING_TOKEN_STORE; "
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

    if store_path:
        TokenStore(Path(store_path)).save(bundle)
    expires_at = datetime.fromtimestamp(bundle.expires_at, tz=timezone.utc)
    print(
        f"token exchanged: {bundle.token_type}, expires_in={bundle.expires_in}s "
        f"(at {expires_at.isoformat()})"
    )
    if store_path:
        print(f"persisted: {store_path} (access token ready to become RING_API_TOKEN)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
