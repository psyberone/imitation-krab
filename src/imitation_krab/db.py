from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import MAX_DATABASE_BYTES, default_db_path, default_pepper_path
from .sanitize import sanitize_text, validate_handle, validate_project_key

TOKEN_RE = re.compile(r"^krab_(usr_[0-9a-f]{32})_([A-Za-z0-9_-]{43})$")
SCHEMA_VERSION = 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    handle TEXT NOT NULL UNIQUE,
    token_digest BLOB NOT NULL,
    token_version INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    project_key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_members (
    project_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('member', 'admin')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, user_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    label TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'closed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE (project_id, id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS session_members (
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, session_id, user_id),
    FOREIGN KEY (project_id, session_id)
        REFERENCES sessions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, user_id)
        REFERENCES project_members(project_id, user_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    risk_flags TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN (
        'open.pending',
        'open.in_progress',
        'open.under_review',
        'open.needs_changes',
        'open.approved',
        'closed.approved',
        'closed.rejected'
    )),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, session_id, id),
    CHECK (creator_id <> recipient_id),
    FOREIGN KEY (project_id, session_id, creator_id)
        REFERENCES session_members(project_id, session_id, user_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, session_id, recipient_id)
        REFERENCES session_members(project_id, session_id, user_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('item.created', 'status.changed', 'item.reminded')),
    from_status TEXT,
    to_status TEXT,
    note TEXT,
    risk_flags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id, session_id, item_id)
        REFERENCES items(project_id, session_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (actor_id) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS deliveries (
    event_seq INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (event_seq, user_id),
    FOREIGN KEY (event_seq) REFERENCES events(seq) ON DELETE RESTRICT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id TEXT,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_project_members_user
    ON project_members(user_id, project_id);
CREATE INDEX IF NOT EXISTS idx_session_members_user
    ON session_members(user_id, project_id, session_id);
CREATE INDEX IF NOT EXISTS idx_items_recipient_status
    ON items(recipient_id, project_id, session_id, status);
CREATE INDEX IF NOT EXISTS idx_items_creator
    ON items(creator_id, project_id, session_id);
CREATE INDEX IF NOT EXISTS idx_events_item
    ON events(item_id, seq);
CREATE INDEX IF NOT EXISTS idx_deliveries_user_event
    ON deliveries(user_id, event_seq);
"""


class DatabaseError(RuntimeError):
    pass


class Database:
    def __init__(
        self, path: Path | str | None = None, pepper_path: Path | str | None = None
    ):
        self.path = Path(path) if path is not None else default_db_path()
        self.pepper_path = (
            Path(pepper_path)
            if pepper_path is not None
            else default_pepper_path(self.path)
        )

    @staticmethod
    def now() -> str:
        return (
            datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}_{secrets.token_hex(16)}"

    def initialize(self) -> None:
        self._ensure_private_parent(self.path.parent)
        self._ensure_pepper()
        self._reject_symlink(self.path, allow_missing=True)
        conn = self.connect(initialize=False)
        try:
            conn.executescript(SCHEMA)
            rows = conn.execute("SELECT version FROM schema_meta").fetchall()
            if not rows:
                conn.execute(
                    "INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif len(rows) != 1 or rows[0]["version"] != SCHEMA_VERSION:
                raise DatabaseError("unsupported database schema version")
            conn.commit()
        finally:
            conn.close()
        os.chmod(self.path, 0o600)

    def connect(self, *, initialize: bool = True) -> sqlite3.Connection:
        if initialize and not self.path.exists():
            self.initialize()
        self._reject_symlink(self.path, allow_missing=not initialize)
        if self.path.exists():
            info = self.path.stat()
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise DatabaseError("database file is not owned by the current user")
            if info.st_mode & 0o077:
                raise DatabaseError(
                    "database file must not be accessible by group or other users"
                )
            if info.st_nlink != 1:
                raise DatabaseError("database file must not be hard-linked")
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA trusted_schema = OFF")
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        max_pages = max(1, MAX_DATABASE_BYTES // page_size)
        conn.execute(f"PRAGMA max_page_count = {max_pages}")
        return conn

    def backup(self, destination: Path | str) -> dict[str, Any]:
        """Create a consistent, private SQLite snapshot without overwriting."""
        target_path = Path(destination)
        self._ensure_private_parent(target_path.parent)
        self._reject_symlink(target_path, allow_missing=True)

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(target_path, flags, 0o600)
        except FileExistsError as exc:
            raise DatabaseError(f"refusing to overwrite backup: {target_path}") from exc
        except OSError as exc:
            raise DatabaseError(f"cannot create backup: {target_path}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DatabaseError("backup target must be a private regular file")
        except Exception:
            os.close(fd)
            with suppress(OSError):
                target_path.unlink()
            raise
        else:
            os.close(fd)

        source: sqlite3.Connection | None = None
        target: sqlite3.Connection | None = None
        try:
            source = self.connect()
            target = sqlite3.connect(target_path, timeout=5.0, isolation_level=None)
            target.execute("PRAGMA trusted_schema = OFF")
            source.backup(target, pages=256, sleep=0.05)
            journal_mode = target.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            if str(journal_mode).lower() != "delete":
                raise DatabaseError("backup could not be made portable")
            check = target.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise DatabaseError("backup integrity check failed")
            target.close()
            target = None
            source.close()
            source = None

            os.chmod(target_path, 0o600)
            read_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                read_flags |= os.O_NOFOLLOW
            verify_fd = os.open(target_path, read_flags)
            try:
                info = os.fstat(verify_fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())
                ):
                    raise DatabaseError("backup is not a private regular file")
                os.fsync(verify_fd)
            finally:
                os.close(verify_fd)
            return {
                "backup": str(target_path),
                "bytes": info.st_size,
                "created_at": self.now(),
            }
        except BaseException:
            for connection in (target, source):
                if connection is not None:
                    with suppress(sqlite3.Error):
                        connection.close()
            for artifact in (
                target_path,
                target_path.with_name(target_path.name + "-wal"),
                target_path.with_name(target_path.name + "-shm"),
                target_path.with_name(target_path.name + "-journal"),
            ):
                with suppress(OSError):
                    artifact.unlink()
            raise

    def create_user(self, handle: str) -> tuple[dict[str, Any], str]:
        handle = validate_handle(handle)
        user_id = self.new_id("usr")
        token = self._new_token(user_id)
        digest = self._token_digest(token)
        now = self.now()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO users(id, handle, token_digest, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, handle, digest, now, now),
            )
            self._audit(conn, None, "user.created", user_id, {"handle": handle})
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise DatabaseError("user handle already exists") from exc
        finally:
            conn.close()
        return {"id": user_id, "handle": handle, "active": True}, token

    def rotate_user_token(self, handle: str) -> tuple[dict[str, Any], str]:
        handle = validate_handle(handle)
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, active, token_version FROM users WHERE handle = ?",
                (handle,),
            ).fetchone()
            if row is None:
                raise DatabaseError("user not found")
            if not row["active"]:
                raise DatabaseError("cannot rotate a disabled user")
            token = self._new_token(row["id"])
            now = self.now()
            conn.execute(
                """
                UPDATE users
                SET token_digest = ?, token_version = token_version + 1, updated_at = ?
                WHERE id = ?
                """,
                (self._token_digest(token), now, row["id"]),
            )
            self._audit(
                conn,
                None,
                "user.token_rotated",
                row["id"],
                {"token_version": row["token_version"] + 1},
            )
            conn.commit()
            return {"id": row["id"], "handle": handle, "active": True}, token
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def disable_user(self, handle: str) -> dict[str, Any]:
        handle = validate_handle(handle)
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM users WHERE handle = ?", (handle,)
            ).fetchone()
            if row is None:
                raise DatabaseError("user not found")
            now = self.now()
            conn.execute(
                "UPDATE users SET active = 0, updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            self._audit(conn, None, "user.disabled", row["id"], {})
            conn.commit()
            return {"id": row["id"], "handle": handle, "active": False}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_project(self, project_key: str, label: str) -> dict[str, Any]:
        project_key = validate_project_key(project_key)
        label_text = sanitize_text(label, field="label", max_bytes=256).text
        project_id = self.new_id("prj")
        now = self.now()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO projects(id, project_key, label, created_at) VALUES (?, ?, ?, ?)",
                (project_id, project_key, label_text, now),
            )
            self._audit(
                conn,
                None,
                "project.created",
                project_id,
                {"project_key": project_key},
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise DatabaseError("project key already exists") from exc
        finally:
            conn.close()
        return {"id": project_id, "key": project_key, "label": label_text}

    def add_project_member(
        self, project_key: str, handle: str, role: str
    ) -> dict[str, Any]:
        project_key = validate_project_key(project_key)
        handle = validate_handle(handle)
        if role not in {"member", "admin"}:
            raise DatabaseError("role must be member or admin")
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute(
                "SELECT id FROM projects WHERE project_key = ?", (project_key,)
            ).fetchone()
            user = conn.execute(
                "SELECT id, active FROM users WHERE handle = ?", (handle,)
            ).fetchone()
            if project is None:
                raise DatabaseError("project not found")
            if user is None or not user["active"]:
                raise DatabaseError("active user not found")
            now = self.now()
            conn.execute(
                """
                INSERT INTO project_members(project_id, user_id, role, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, user_id) DO UPDATE SET role = excluded.role
                """,
                (project["id"], user["id"], role, now),
            )
            self._audit(
                conn,
                None,
                "project.member_set",
                project["id"],
                {"user_id": user["id"], "role": role},
            )
            conn.commit()
            return {
                "project_key": project_key,
                "user_id": user["id"],
                "handle": handle,
                "role": role,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def authenticate(self, token: str) -> dict[str, Any] | None:
        match = TOKEN_RE.fullmatch(token)
        if match is None:
            return None
        user_id = match.group(1)
        supplied_digest = self._token_digest(token)
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT id, handle, token_digest, token_version FROM users WHERE id = ? AND active = 1",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        expected_digest = row["token_digest"] if row is not None else bytes(32)
        digest_matches = hmac.compare_digest(expected_digest, supplied_digest)
        if row is None or not digest_matches:
            return None
        return {
            "id": row["id"],
            "handle": row["handle"],
            "token_version": row["token_version"],
        }

    def _new_token(self, user_id: str) -> str:
        secret = (
            base64.urlsafe_b64encode(secrets.token_bytes(32))
            .rstrip(b"=")
            .decode("ascii")
        )
        return f"krab_{user_id}_{secret}"

    def _token_digest(self, token: str) -> bytes:
        return hmac.new(
            self._read_pepper(), token.encode("utf-8"), hashlib.sha256
        ).digest()

    def _ensure_pepper(self) -> None:
        self._ensure_private_parent(self.pepper_path.parent)
        self._reject_symlink(self.pepper_path, allow_missing=True)
        if not self.pepper_path.exists():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.pepper_path, flags, 0o600)
            try:
                os.write(fd, secrets.token_bytes(32))
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            info = self.pepper_path.lstat()
            if info.st_nlink != 1:
                raise DatabaseError("pepper file must not be hard-linked")
        os.chmod(self.pepper_path, 0o600)

    def _read_pepper(self) -> bytes:
        self._ensure_pepper()
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.pepper_path, flags)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            ):
                raise DatabaseError("pepper file must be a private regular file")
            value = os.read(fd, 64)
        finally:
            os.close(fd)
        if len(value) != 32:
            raise DatabaseError("pepper file has an invalid length")
        return value

    @staticmethod
    def _reject_symlink(path: Path, *, allow_missing: bool) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise
        if stat.S_ISLNK(info.st_mode):
            raise DatabaseError(f"refusing symlink path: {path}")
        if not stat.S_ISREG(info.st_mode):
            raise DatabaseError(f"expected a regular file: {path}")

    @staticmethod
    def _ensure_private_parent(path: Path) -> None:
        if not path.exists():
            path.mkdir(mode=0o700, parents=True)
            os.chmod(path, 0o700)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise DatabaseError(f"state parent is not a private directory: {path}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise DatabaseError(
                f"state parent is not owned by the current user: {path}"
            )
        if info.st_mode & 0o077:
            raise DatabaseError(f"state parent must use mode 0700: {path}")

    def _audit(
        self,
        conn: sqlite3.Connection,
        actor_user_id: str | None,
        action: str,
        target: str,
        details: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_log(actor_user_id, action, target, details_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                actor_user_id,
                action,
                target,
                json.dumps(details, separators=(",", ":"), sort_keys=True),
                self.now(),
            ),
        )
