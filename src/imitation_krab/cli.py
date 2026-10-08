from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import sqlite3
import stat
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from . import __version__
from .config import (
    CONTAINER_HOST,
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_CLAIM_IMPORT_ENTRIES,
    MAX_REQUEST_BYTES,
    default_db_path,
    default_pepper_path,
    default_server_url,
    default_token_file,
)
from .db import TOKEN_RE, Database, DatabaseError
from .http_api import serve
from .sanitize import ValidationError, terminal_lines, validate_external_id

STATUS_ALIASES = {
    "pending": "open.pending",
    "in-progress": "open.in_progress",
    "under-review": "open.under_review",
    "needs-changes": "open.needs_changes",
    "approved": "open.approved",
    "closed-approved": "closed.approved",
    "closed-rejected": "closed.rejected",
}

CLAIM_KIND_PATHS = {"issue": "issues", "pr": "pull-requests"}
CLAIM_STATUS_ALIASES = {
    "available": "available",
    "claimed": "claimed",
    "active": "active",
    "under-review": "under_review",
    "done": "done",
}


class ClientError(RuntimeError):
    pass


class APIClient:
    def __init__(self, base_url: str, token: str, idempotency_key: str | None = None):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ClientError(
                "server URL must be an HTTP loopback origin without credentials or a path"
            )
        try:
            self.port = parsed.port or DEFAULT_PORT
        except ValueError as exc:
            raise ClientError("server URL contains an invalid port") from exc
        self.host = parsed.hostname
        self.token = token
        self.idempotency_key = idempotency_key

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = path
        if query:
            target += "?" + urlencode(query)
        body = None
        request_key: str | None = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": f"imitation-krab/{__version__}",
        }
        if payload is not None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
            request_key = self.idempotency_key or uuid.uuid4().hex
            headers["Idempotency-Key"] = request_key
        connection = http.client.HTTPConnection(self.host, self.port, timeout=40)
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            retry = (
                f"; mutation outcome may be unknown; retry with --idempotency-key {request_key}"
                if request_key
                else ""
            )
            raise ClientError(f"server unavailable: {exc}{retry}") from None
        finally:
            connection.close()
        if status >= 400:
            try:
                parsed = json.loads(raw.decode("utf-8"))
                message = parsed.get("error", {}).get("message", f"HTTP {status}")
                code = parsed.get("error", {}).get("code", "http_error")
                details = parsed.get("error", {}).get("details")
            except (UnicodeError, json.JSONDecodeError, AttributeError):
                code, message, details = "http_error", f"HTTP {status}", None
            suffix = (
                "; details="
                + json.dumps(
                    details, ensure_ascii=True, separators=(",", ":"), sort_keys=True
                )
                if isinstance(details, dict)
                else ""
            )
            raise ClientError(f"{code}: {message}{suffix}") from None
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            retry = (
                f"; retry with --idempotency-key {request_key}" if request_key else ""
            )
            raise ClientError(f"server returned invalid JSON{retry}") from exc
        if not isinstance(result, dict):
            raise ClientError("server returned an unexpected response")
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="krab",
        description="Local, scoped work mailbox for cooperating software agents",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )
    parser.add_argument(
        "--db", type=Path, default=default_db_path(), help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--pepper-file", type=Path, default=None, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--server", default=default_server_url(), help="server base URL"
    )
    parser.add_argument("--token-file", type=Path, default=default_token_file())
    parser.add_argument(
        "--idempotency-key",
        help="reuse this key when safely retrying one mutation after an uncertain response",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="initialize the private database and pepper")

    serve_parser = sub.add_parser("serve", help="run the local daemon")
    serve_parser.add_argument("--host", default=DEFAULT_HOST, choices=[DEFAULT_HOST])
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument(
        "--container-bind",
        action="store_true",
        help="bind container interfaces; publish the port only on host loopback",
    )
    serve_parser.add_argument("--verbose", action="store_true")

    admin = sub.add_parser("admin", help="local administrative operations")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    user_create = admin_sub.add_parser("user-create")
    user_create.add_argument("handle")
    user_create.add_argument("--write-token", type=Path)
    user_rotate = admin_sub.add_parser("user-rotate")
    user_rotate.add_argument("handle")
    user_rotate.add_argument("--write-token", type=Path)
    user_disable = admin_sub.add_parser("user-disable")
    user_disable.add_argument("handle")
    admin_sub.add_parser("user-list", help="list local identities without credentials")
    project_create = admin_sub.add_parser("project-create")
    project_create.add_argument("key")
    project_create.add_argument("--label", required=True)
    admin_sub.add_parser("project-list", help="list all local projects")
    project_members = admin_sub.add_parser(
        "project-members", help="list local project membership"
    )
    project_members.add_argument("project")
    project_member = admin_sub.add_parser("project-add-user")
    project_member.add_argument("project")
    project_member.add_argument("handle")
    project_member.add_argument(
        "--role", choices=["member", "coordinator", "admin"], default="member"
    )
    backup = admin_sub.add_parser(
        "backup", help="create a live-safe SQLite snapshot without overwriting"
    )
    backup.add_argument("destination", type=Path)

    sub.add_parser("projects", help="list authenticated user's projects")

    issues = sub.add_parser("issues", help="list the project's issue claim queue")
    _add_claim_list_args(issues)

    pull_requests = sub.add_parser(
        "prs", help="list the project's pull-request claim queue"
    )
    _add_claim_list_args(pull_requests)

    claim_add = sub.add_parser(
        "claim-add", help="register an issue or pull request as available work"
    )
    claim_add.add_argument("external_id")
    _add_claim_scope_args(claim_add)

    claim_import = sub.add_parser(
        "claim-import",
        help="add missing issue or pull-request identifiers from a JSON array",
    )
    claim_import.add_argument("manifest", help="JSON file, or - for standard input")
    _add_claim_scope_args(claim_import)
    claim_import.add_argument(
        "--dry-run", action="store_true", help="show additions without changing state"
    )

    claim = sub.add_parser("claim", help="atomically claim work for yourself")
    claim.add_argument("claim_id")
    _add_claim_scope_args(claim)
    claim.add_argument("--expected-version", type=int, required=True)

    claim_assign = sub.add_parser(
        "claim-assign", help="assign available work as a coordinator or admin"
    )
    claim_assign.add_argument("claim_id")
    _add_claim_scope_args(claim_assign)
    claim_assign.add_argument("--to", required=True)
    claim_assign.add_argument("--expected-version", type=int, required=True)

    claim_release = sub.add_parser(
        "claim-release",
        help="release your claim, or release one as coordinator or admin",
    )
    claim_release.add_argument("claim_id")
    _add_claim_scope_args(claim_release)
    claim_release.add_argument("--expected-version", type=int, required=True)

    claim_status = sub.add_parser(
        "claim-status", help="activate or submit assigned work"
    )
    claim_status.add_argument("claim_id")
    claim_status.add_argument("status", choices=["active", "under-review"])
    _add_claim_scope_args(claim_status)
    claim_status.add_argument("--expected-version", type=int, required=True)

    claim_review = sub.add_parser(
        "claim-review",
        help="atomically record a reviewer verdict and notify the assignee",
    )
    claim_review.add_argument("claim_id")
    claim_review.add_argument("action", choices=["changes-requested", "approved"])
    _add_claim_scope_args(claim_review)
    claim_review.add_argument("--session", required=True)
    claim_review.add_argument("--expected-version", type=int, required=True)
    _add_text_source(claim_review, "body", required=True)

    claim_show = sub.add_parser(
        "claim-show", help="show a claim and its trusted review history"
    )
    claim_show.add_argument("claim_id")
    _add_claim_scope_args(claim_show)

    sessions = sub.add_parser("sessions", help="list sessions in a project")
    sessions.add_argument("--project", required=True)

    session_create = sub.add_parser(
        "session-create", help="create a scoped work session"
    )
    session_create.add_argument("--project", required=True)
    session_create.add_argument("--label", required=True)
    session_create.add_argument("--participant", action="append", required=True)

    session_close = sub.add_parser(
        "session-close", help="close an empty/completed session"
    )
    session_close.add_argument("--project", required=True)
    session_close.add_argument("--session", required=True)

    send = sub.add_parser(
        "send", help="place a work item in another participant's queue"
    )
    _add_scope_args(send)
    send.add_argument("--to", required=True)
    send.add_argument("--title", required=True)
    send.add_argument("--claim", help="immutably link an issue or pull-request claim")
    _add_text_source(send, "body", required=True)

    queue = sub.add_parser("queue", help="read the authenticated user's private queue")
    _add_scope_args(queue)
    queue.add_argument("--after", type=int, default=0)
    queue.add_argument("--limit", type=int, default=50)
    queue_wait = queue.add_mutually_exclusive_group()
    queue_wait.add_argument("--watch", action="store_true")
    queue_wait.add_argument("--wait", type=int, default=0)

    inbox = sub.add_parser(
        "inbox", help="read private deliveries across every session in a project"
    )
    inbox.add_argument("--project", required=True)
    inbox_cursor = inbox.add_mutually_exclusive_group()
    inbox_cursor.add_argument("--after", type=int)
    inbox_cursor.add_argument("--unread", action="store_true")
    inbox.add_argument("--limit", type=int, default=50)
    inbox_wait = inbox.add_mutually_exclusive_group()
    inbox_wait.add_argument("--watch", action="store_true")
    inbox_wait.add_argument("--wait", type=int, default=0)

    inbox_ack = sub.add_parser(
        "inbox-ack", help="explicitly advance the durable project inbox cursor"
    )
    inbox_ack.add_argument("--project", required=True)
    inbox_ack.add_argument("--through", type=int, required=True)

    show = sub.add_parser("show", help="show an accessible work item and history")
    _add_scope_args(show)
    show.add_argument("item")

    status = sub.add_parser("status", help="perform an authorized status transition")
    _add_scope_args(status)
    status.add_argument("item")
    status.add_argument("status", choices=sorted(STATUS_ALIASES))
    status.add_argument("--expected-version", type=int, required=True)
    _add_text_source(status, "note", required=False)

    notify = sub.add_parser(
        "notify", help="send a bodyless, rate-limited work-item reminder"
    )
    _add_scope_args(notify)
    notify.add_argument("item")
    return parser


def _add_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--session", required=True)


def _add_claim_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--kind", choices=sorted(CLAIM_KIND_PATHS), required=True)


def _add_claim_list_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--status", choices=sorted(CLAIM_STATUS_ALIASES))
    parser.add_argument("--assignee")


def _add_text_source(
    parser: argparse.ArgumentParser, name: str, *, required: bool
) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(f"--{name}")
    group.add_argument(f"--{name}-file", type=Path)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pepper_path = args.pepper_file or default_pepper_path(args.db)
    database = Database(args.db, pepper_path)
    try:
        if args.command == "init":
            database.initialize()
            _emit(args, {"database": str(database.path), "initialized": True})
            return 0
        if args.command == "serve":
            logging.basicConfig(
                level=logging.DEBUG if args.verbose else logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s %(message)s",
            )
            host = CONTAINER_HOST if args.container_bind else args.host
            serve(
                database,
                host,
                args.port,
                container_bind=args.container_bind,
            )
            return 0
        if args.command == "admin":
            return _run_admin(args, database)

        client = APIClient(
            args.server,
            _read_token(args.token_file),
            idempotency_key=args.idempotency_key,
        )
        return _run_client(args, client)
    except KeyboardInterrupt:
        return 130
    except (ClientError, DatabaseError, OSError, ValueError, sqlite3.Error) as exc:
        _safe_error(str(exc))
        return 1


def _run_admin(args: argparse.Namespace, database: Database) -> int:
    command = args.admin_command
    if command == "user-create":
        user, token = database.create_user(args.handle)
        _emit_token_result(args, user, token, args.write_token, replace=False)
    elif command == "user-rotate":
        user, token = database.rotate_user_token(args.handle)
        _emit_token_result(args, user, token, args.write_token, replace=True)
    elif command == "user-disable":
        _emit(args, database.disable_user(args.handle))
    elif command == "user-list":
        _emit(args, database.list_users())
    elif command == "project-create":
        _emit(args, database.create_project(args.key, args.label))
    elif command == "project-list":
        _emit(args, database.list_projects_inventory())
    elif command == "project-members":
        _emit(args, database.list_project_members(args.project))
    elif command == "project-add-user":
        _emit(
            args,
            database.add_project_member(args.project, args.handle, args.role),
        )
    elif command == "backup":
        _emit(args, database.backup(args.destination))
    else:
        raise ValueError("unknown administrative command")
    return 0


def _run_client(args: argparse.Namespace, client: APIClient) -> int:
    command = args.command
    if command == "projects":
        result = client.request("GET", "/v1/projects")
        _emit(args, result, kind="projects")
    elif command in {"issues", "prs"}:
        queue = "issues" if command == "issues" else "pull-requests"
        query = {}
        if args.status is not None:
            query["status"] = CLAIM_STATUS_ALIASES[args.status]
        if args.assignee is not None:
            query["assignee"] = args.assignee
        result = client.request(
            "GET",
            f"/v1/projects/{_segment(args.project)}/{queue}",
            query=query or None,
        )
        _emit(args, result, kind="claims")
    elif command == "claim-add":
        result = client.request(
            "POST",
            _claim_queue_path(args),
            payload={"external_id": args.external_id},
        )
        _emit(args, result, kind="claim")
    elif command == "claim-import":
        external_ids = _read_claim_manifest(args.manifest)
        if args.dry_run:
            current = client.request("GET", _claim_queue_path(args))
            existing = {
                claim["trusted_metadata"]["external_id"]
                for claim in current.get("claims", [])
            }
            missing = [value for value in external_ids if value not in existing]
            result = {
                "trusted_metadata": {
                    "project_key": args.project,
                    "kind": "pull_request" if args.kind == "pr" else "issue",
                    "requested_count": len(external_ids),
                    "would_create_count": len(missing),
                    "unchanged_count": len(external_ids) - len(missing),
                    "dry_run": True,
                },
                "would_create": missing,
            }
        else:
            result = client.request(
                "POST",
                _claim_queue_path(args) + "/import",
                payload={"external_ids": external_ids},
            )
        _emit(args, result, kind="claim-import")
    elif command == "claim":
        result = client.request(
            "POST",
            _claim_path(args) + "/claim",
            payload={"expected_version": args.expected_version},
        )
        _emit(args, result, kind="claim")
    elif command == "claim-assign":
        result = client.request(
            "POST",
            _claim_path(args) + "/assign",
            payload={
                "assignee": args.to,
                "expected_version": args.expected_version,
            },
        )
        _emit(args, result, kind="claim")
    elif command == "claim-release":
        result = client.request(
            "POST",
            _claim_path(args) + "/release",
            payload={"expected_version": args.expected_version},
        )
        _emit(args, result, kind="claim")
    elif command == "claim-status":
        result = client.request(
            "PATCH",
            _claim_path(args) + "/status",
            payload={
                "status": CLAIM_STATUS_ALIASES[args.status],
                "expected_version": args.expected_version,
            },
        )
        _emit(args, result, kind="claim")
    elif command == "claim-review":
        result = client.request(
            "POST",
            _claim_path(args) + "/review",
            payload={
                "action": args.action.replace("-", "_"),
                "expected_version": args.expected_version,
                "session_id": args.session,
                "body": _text_argument(args, "body"),
            },
        )
        _emit(args, result, kind="review")
    elif command == "claim-show":
        result = client.request("GET", _claim_path(args))
        _emit(args, result, kind="claim-detail")
    elif command == "sessions":
        result = client.request(
            "GET", f"/v1/projects/{_segment(args.project)}/sessions"
        )
        _emit(args, result, kind="sessions")
    elif command == "session-create":
        result = client.request(
            "POST",
            f"/v1/projects/{_segment(args.project)}/sessions",
            payload={"label": args.label, "participants": args.participant},
        )
        _emit(args, result, kind="session")
    elif command == "session-close":
        result = client.request(
            "POST",
            f"/v1/projects/{_segment(args.project)}/sessions/{_segment(args.session)}/close",
            payload={},
        )
        _emit(args, result)
    elif command == "send":
        body = _text_argument(args, "body")
        send_payload: dict[str, Any] = {
            "to": args.to,
            "title": args.title,
            "body": body,
        }
        if args.claim is not None:
            send_payload["claim_id"] = args.claim
        result = client.request(
            "POST",
            _scope_path(args) + "/items",
            payload=send_payload,
        )
        _emit(args, result, kind="item")
    elif command == "queue":
        cursor = args.after
        while True:
            result = client.request(
                "GET",
                _scope_path(args) + "/queue",
                query={
                    "after": cursor,
                    "limit": args.limit,
                    "wait": 30 if args.watch else args.wait,
                },
            )
            _emit(args, result, kind="queue")
            cursor = result["trusted_metadata"]["next_cursor"]
            if not args.watch:
                break
    elif command == "inbox":
        cursor = args.after
        first = True
        while True:
            query: dict[str, Any] = {
                "limit": args.limit,
                "wait": 30 if args.watch else args.wait,
            }
            if first and args.unread:
                query["unread"] = 1
            else:
                query["after"] = cursor if cursor is not None else 0
            result = client.request(
                "GET",
                f"/v1/projects/{_segment(args.project)}/inbox",
                query=query,
            )
            _emit(args, result, kind="queue")
            cursor = result["trusted_metadata"]["next_cursor"]
            first = False
            if not args.watch:
                break
    elif command == "inbox-ack":
        result = client.request(
            "POST",
            f"/v1/projects/{_segment(args.project)}/inbox/ack",
            payload={"through": args.through},
        )
        _emit(args, result)
    elif command == "show":
        result = client.request(
            "GET", _scope_path(args) + f"/items/{_segment(args.item)}"
        )
        _emit(args, result, kind="item")
    elif command == "status":
        payload: dict[str, Any] = {
            "status": STATUS_ALIASES[args.status],
            "expected_version": args.expected_version,
        }
        note = _text_argument(args, "note")
        if note is not None:
            payload["note"] = note
        result = client.request(
            "PATCH",
            _scope_path(args) + f"/items/{_segment(args.item)}/status",
            payload=payload,
        )
        _emit(args, result, kind="item")
    elif command == "notify":
        result = client.request(
            "POST",
            _scope_path(args) + f"/items/{_segment(args.item)}/notify",
            payload={},
        )
        _emit(args, result)
    else:
        raise ValueError("unknown command")
    return 0


def _scope_path(args: argparse.Namespace) -> str:
    return f"/v1/projects/{_segment(args.project)}/sessions/{_segment(args.session)}"


def _claim_queue_path(args: argparse.Namespace) -> str:
    return f"/v1/projects/{_segment(args.project)}/{CLAIM_KIND_PATHS[args.kind]}"


def _claim_path(args: argparse.Namespace) -> str:
    return _claim_queue_path(args) + f"/{_segment(args.claim_id)}"


def _segment(value: str) -> str:
    return quote(value, safe="")


def _text_argument(args: argparse.Namespace, name: str) -> str | None:
    direct = getattr(args, name, None)
    file_path: Path | None = getattr(args, f"{name}_file", None)
    if direct is not None:
        return direct
    if file_path is not None:
        return file_path.read_text(encoding="utf-8")
    return None


def _read_claim_manifest(source: str) -> list[str]:
    if source == "-":
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    else:
        with Path(source).open("rb") as stream:
            raw = stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ClientError(f"claim manifest exceeds {MAX_REQUEST_BYTES} bytes")
    try:
        text = raw.decode("utf-8", "strict")

        def reject_constant(value: str) -> None:
            raise ValueError(f"invalid JSON number: {value}")

        def reject_float(_value: str) -> None:
            raise ValueError("floating-point JSON numbers are not accepted")

        payload = json.loads(
            text,
            parse_constant=reject_constant,
            parse_float=reject_float,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ClientError("claim manifest must be valid UTF-8 JSON") from exc
    if not isinstance(payload, list):
        raise ClientError("claim manifest must be a JSON array of identifiers")
    if len(payload) > MAX_CLAIM_IMPORT_ENTRIES:
        raise ClientError(
            f"claim manifest may contain at most {MAX_CLAIM_IMPORT_ENTRIES} identifiers"
        )
    external_ids: list[str] = []
    seen: set[str] = set()
    for value in payload:
        try:
            external_id = validate_external_id(value)
        except ValidationError as exc:
            raise ClientError(str(exc)) from exc
        if external_id in seen:
            raise ClientError("claim manifest contains duplicate identifiers")
        seen.add(external_id)
        external_ids.append(external_id)
    return external_ids


def _read_token(path: Path) -> str:
    _ensure_private_token_parent(path.parent)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ClientError(f"token file not found: {path}") from exc
    except OSError as exc:
        raise ClientError("token file must be a regular file, not a symlink") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ClientError("token file must be a regular file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ClientError("token file is not owned by the current user")
        if info.st_nlink != 1:
            raise ClientError("token file must not be hard-linked")
        if info.st_mode & 0o077:
            raise ClientError(
                "token file must not be accessible by group or other users"
            )
        raw = os.read(fd, 512)
        if os.read(fd, 1):
            raise ClientError("token file is unexpectedly large")
    finally:
        os.close(fd)
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ClientError("token file is invalid") from exc
    if TOKEN_RE.fullmatch(token) is None:
        raise ClientError("token file does not contain an imitation-krab token")
    return token


def _write_token(path: Path, token: str, *, replace: bool) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _ensure_private_token_parent(path.parent)
    if not replace:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise ClientError(f"refusing to overwrite token file: {path}") from exc
        try:
            os.write(fd, token.encode("ascii") + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
        return

    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            raise ClientError("existing token path is not a regular file")
    fd, temporary = tempfile.mkstemp(prefix=".token-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(token.encode("ascii") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _ensure_private_token_parent(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ClientError("token parent must be a private directory, not a symlink")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ClientError("token parent is not owned by the current user")
    if info.st_mode & 0o077:
        raise ClientError("token parent directory must use mode 0700")


def _emit_token_result(
    args: argparse.Namespace,
    user: dict[str, Any],
    token: str,
    token_path: Path | None,
    *,
    replace: bool,
) -> None:
    if token_path:
        _write_token(token_path, token, replace=replace)
        result = {
            "user": user,
            "credential_file": str(token_path),
            "credential_delivery": "file",
        }
    else:
        result = {"user": user, "token": token, "credential_delivery": "stdout"}
    _emit(args, result)


def _emit(
    args: argparse.Namespace, payload: dict[str, Any], kind: str | None = None
) -> None:
    if args.json:
        print(
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
        )
        return
    if kind == "projects":
        for project in payload.get("projects", []):
            meta = project["trusted_metadata"]
            print(f"{meta['project_key']}  [{meta['role']}]  {meta['project_id']}")
            _print_untrusted(
                project["untrusted_text"].get("label", ""),
                project.get("content_risk_flags", []),
                field="label",
            )
        return
    if kind == "sessions":
        for session in payload.get("sessions", []):
            meta = session["trusted_metadata"]
            print(
                f"{meta['session_id']}  [{meta['state']}]  created={meta['created_at']}"
            )
            _print_untrusted(
                session["untrusted_text"].get("label", ""),
                session.get("content_risk_flags", []),
                field="label",
            )
        return
    if kind == "session":
        meta = payload["trusted_metadata"]
        print(f"[{meta['project_key']}/{meta['session_id']}] {meta['state']}")
        _print_untrusted(
            payload["untrusted_text"].get("label", ""),
            payload.get("content_risk_flags", []),
            field="label",
        )
        return
    if kind == "claims":
        for claim in payload.get("claims", []):
            _print_claim(claim)
        return
    if kind == "claim-import":
        meta = payload["trusted_metadata"]
        action = "would-create" if meta.get("dry_run") else "created"
        count_key = "would_create_count" if meta.get("dry_run") else "created_count"
        print(
            f"[{meta['project_key']}] import {meta['kind']}: "
            f"requested={meta['requested_count']} {action}={meta[count_key]} "
            f"unchanged={meta['unchanged_count']}"
        )
        for claim in payload.get("created", []):
            _print_claim(claim)
        for external_id in payload.get("would_create", []):
            print(f"  would create {external_id}")
        return
    if kind == "claim":
        _print_claim(payload)
        return
    if kind == "review":
        meta = payload["trusted_metadata"]
        print(f"review {meta['action']} round={meta['review_round']}")
        _print_claim(payload["claim"])
        _print_item(payload["notification"])
        return
    if kind == "claim-detail":
        _print_claim(payload["claim"])
        for event in payload.get("review_history", []):
            meta = event["trusted_metadata"]
            actor = meta["actor"]["handle"]
            transition = (
                f"{meta.get('from_status') or '-'} -> {meta.get('to_status') or '-'}"
            )
            print(
                f"  review round={meta['review_round']} {meta['action']} "
                f"by {actor} {transition} at {meta['created_at']}"
            )
        return
    if kind == "item":
        _print_item(payload)
        return
    if kind == "queue":
        for event in payload.get("events", []):
            _print_event(event)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _print_item(payload: dict[str, Any]) -> None:
    meta = payload["trusted_metadata"]
    print(
        f"[{meta['project_key']}/{meta['session_id']}] {meta['item_id']} "
        f"{meta['status']} v{meta['version']}"
    )
    print(f"  {meta['creator']['handle']} -> {meta['recipient']['handle']}")
    if meta.get("work_claim") is not None:
        work = meta["work_claim"]
        print(f"  linked {work['kind']} {work['external_id']} ({work['claim_id']})")
    flags = payload.get("content_risk_flags", [])
    if flags:
        print("  CONTENT WARNING: " + ", ".join(flags))
    text = payload.get("untrusted_text", {})
    _print_untrusted(text.get("title", ""), [], field="title")
    _print_untrusted(text.get("body", ""), [], field="body")
    for event in payload.get("history", []):
        _print_event(event)


def _print_claim(payload: dict[str, Any]) -> None:
    meta = payload["trusted_metadata"]
    assignee = meta["assignee"]["handle"] if meta["assignee"] else "-"
    print(
        f"[{meta['project_key']}] {meta['kind']} {meta['external_id']} "
        f"[{meta['status']}] v{meta['version']} assignee={assignee} "
        f"{meta['claim_id']}"
    )


def _print_event(event: dict[str, Any]) -> None:
    meta = event["trusted_metadata"]
    scope = ""
    if "project_key" in meta:
        scope = f"[{meta['project_key']}/{meta['session_id']}] "
    actor = meta.get("actor", {}).get("handle", "system")
    transition = ""
    if meta.get("to_status"):
        transition = f" {meta.get('from_status') or '-'} -> {meta['to_status']}"
    print(f"{scope}event#{meta['event_seq']} {meta['kind']} by {actor}{transition}")
    if meta.get("work_claim") is not None:
        work = meta["work_claim"]
        print(f"  linked {work['kind']} {work['external_id']} ({work['claim_id']})")
    flags = event.get("content_risk_flags", [])
    if flags:
        print("  CONTENT WARNING: " + ", ".join(flags))
    for field in ("title", "body", "note"):
        value = event.get("untrusted_text", {}).get(field)
        if value:
            _print_untrusted(value, [], field=field)


def _print_untrusted(value: str, flags: list[str], *, field: str) -> None:
    if flags:
        print("  CONTENT WARNING: " + ", ".join(flags))
    print(f"  untrusted_text.{field}:")
    for line in terminal_lines(value, prefix="    "):
        print(line)


def _safe_error(message: str) -> None:
    print("error:", file=sys.stderr)
    for line in terminal_lines(message):
        print(line, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
