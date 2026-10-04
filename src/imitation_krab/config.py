from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "imitation-krab"
DEFAULT_HOST = "127.0.0.1"
# Explicit opt-in for bridge networking; validate_bind_host guards every use.
CONTAINER_HOST = "0.0.0.0"  # nosec B104
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 32 * 1024
MAX_TITLE_BYTES = 512
MAX_BODY_BYTES = 16 * 1024
MAX_NOTE_BYTES = 4 * 1024
MAX_LABEL_BYTES = 256
MAX_EXTERNAL_ID_BYTES = 256
MAX_QUEUE_LIMIT = 100
MAX_LONG_POLL_SECONDS = 30
MAX_LONG_POLLS_PER_USER = 2
MAX_OPEN_ITEMS_PER_ROUTE = 100
MAX_ACTIVE_SESSIONS_PER_USER = 50
MAX_EVENTS_PER_ITEM = 128
MAX_WORK_CLAIMS_PER_PROJECT = 10_000
MAX_DATABASE_BYTES = 256 * 1024 * 1024
REMINDER_COOLDOWN_SECONDS = 60


def default_state_dir() -> Path:
    override = os.environ.get("KRAB_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / APP_NAME
    return Path.home() / ".local" / "state" / APP_NAME


def default_db_path() -> Path:
    override = os.environ.get("KRAB_DB")
    if override:
        return Path(override).expanduser()
    return default_state_dir() / "krab.db"


def default_pepper_path(db_path: Path | None = None) -> Path:
    override = os.environ.get("KRAB_PEPPER_FILE")
    if override:
        return Path(override).expanduser()
    return (db_path or default_db_path()).parent / "pepper.key"


def default_token_file() -> Path:
    override = os.environ.get("KRAB_TOKEN_FILE")
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / APP_NAME / "token"


def default_server_url() -> str:
    return os.environ.get(
        "KRAB_SERVER", f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
    ).rstrip("/")
