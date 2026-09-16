"""Configuration from the environment, with a stdlib-only ``.env`` loader.

Design rules:
- The core stays dependency-free: no ``python-dotenv``. A minimal
  KEY=VALUE parser covers the ``.env`` convention (comments, ``export``
  prefix, single/double-quoted values).
- The real environment always wins: a variable already set in the
  process environment is never overridden by the ``.env`` file.
- Secrets are never defaulted, never logged, and never hardcoded.
  ``.env`` is gitignored; ``.env.example`` documents the contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_ENV_FILE = ".env"

DEFAULT_SIGNATURE_HEADER = "x-ring-signature"
DEFAULT_NIGHT_START = 21  # inclusive, event-local hour (UTC in this skeleton)
DEFAULT_NIGHT_END = 6  # exclusive
DEFAULT_MOTION_BURST_COUNT = 3
DEFAULT_MOTION_BURST_WINDOW_MIN = 10
DEFAULT_LLM_TIMEOUT_S = 20.0
DEFAULT_TELEGRAM_TIMEOUT_S = 10.0
DEFAULT_DB_PATH = "ring-assistant.db"
DEFAULT_API_BASE_URL = "https://api.amazonvision.com"  # documented Events API host
DEFAULT_API_TIMEOUT_S = 10.0


def load_env_file(path: str | Path = DEFAULT_ENV_FILE) -> bool:
    """Load ``KEY=VALUE`` lines from ``path`` into ``os.environ``.

    Returns True when the file existed (regardless of whether any new
    variable was set). Missing or unreadable file -> False, no error:
    running without a ``.env`` is a supported, ordinary mode.
    """
    file = Path(path)
    if not file.is_file():
        return False
    try:
        lines = file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
    return True


def _get_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Every knob the pipeline reads, with offline-safe defaults.

    ``webhook_secret`` and ``llm_*`` default to empty: the pipeline
    refuses to verify or call out with empty credentials rather than
    silently degrading. The demo CLI passes an explicitly-labeled
    synthetic secret of its own.
    """

    webhook_secret: str = ""
    signature_header: str = DEFAULT_SIGNATURE_HEADER
    llm_endpoint: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_s: float = DEFAULT_LLM_TIMEOUT_S
    notify_webhook_url: str = ""
    # Telegram sink (S3 value layer; empty = sink not wired / mock mode)
    telegram_bot_token: str = ""  # TELEGRAM_BOT_TOKEN (.env only)
    telegram_chat_id: str = ""  # TELEGRAM_CHAT_ID (.env only)
    telegram_timeout_s: float = DEFAULT_TELEGRAM_TIMEOUT_S
    night_start_hour: int = DEFAULT_NIGHT_START
    night_end_hour: int = DEFAULT_NIGHT_END
    motion_burst_count: int = DEFAULT_MOTION_BURST_COUNT
    motion_burst_window_min: int = DEFAULT_MOTION_BURST_WINDOW_MIN
    db_path: str = DEFAULT_DB_PATH
    # S2 real-ingestion knobs (all optional; empty = offline mode)
    record_dir: str = ""  # RING_RECORD_DIR -> webhooks.jsonl capture
    api_base_url: str = DEFAULT_API_BASE_URL  # Events API (snapshots/history)
    api_token: str = ""  # RING_API_TOKEN (registration day; .env only)
    api_timeout_s: float = DEFAULT_API_TIMEOUT_S

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from ``env`` (defaults to the process environment).

        Callers that want ``.env`` support run :func:`load_env_file`
        first; the loader never overrides real environment variables.
        """
        source = os.environ if env is None else env
        return cls(
            webhook_secret=source.get("RING_WEBHOOK_SECRET", ""),
            signature_header=source.get(
                "RING_SIGNATURE_HEADER", DEFAULT_SIGNATURE_HEADER
            ).lower(),
            llm_endpoint=source.get("RING_LLM_ENDPOINT", ""),
            llm_model=source.get("RING_LLM_MODEL", ""),
            llm_api_key=source.get("RING_LLM_API_KEY", ""),
            llm_timeout_s=_get_float(source, "RING_LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT_S),
            notify_webhook_url=source.get("RING_NOTIFY_WEBHOOK_URL", ""),
            telegram_bot_token=source.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=source.get("TELEGRAM_CHAT_ID", ""),
            telegram_timeout_s=_get_float(
                source, "TELEGRAM_TIMEOUT", DEFAULT_TELEGRAM_TIMEOUT_S
            ),
            night_start_hour=_get_int(
                source, "RING_NIGHT_START", DEFAULT_NIGHT_START
            ),
            night_end_hour=_get_int(source, "RING_NIGHT_END", DEFAULT_NIGHT_END),
            motion_burst_count=_get_int(
                source, "RING_MOTION_BURST_COUNT", DEFAULT_MOTION_BURST_COUNT
            ),
            motion_burst_window_min=_get_int(
                source,
                "RING_MOTION_BURST_WINDOW_MIN",
                DEFAULT_MOTION_BURST_WINDOW_MIN,
            ),
            db_path=source.get("RING_DB_PATH", DEFAULT_DB_PATH),
            record_dir=source.get("RING_RECORD_DIR", ""),
            api_base_url=source.get("RING_API_BASE_URL", DEFAULT_API_BASE_URL),
            api_token=source.get("RING_API_TOKEN", ""),
            api_timeout_s=_get_float(source, "RING_API_TIMEOUT", DEFAULT_API_TIMEOUT_S),
        )
