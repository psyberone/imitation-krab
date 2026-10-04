from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .config import (
    CONTAINER_HOST,
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_LONG_POLLS_PER_USER,
    MAX_REQUEST_BYTES,
)
from .db import Database
from .service import MutationResult, Service, ServiceError

LOG = logging.getLogger("imitation_krab.http")


class JsonRequestError(ValueError):
    pass


def decode_json_object(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise JsonRequestError(
            f"request body must contain 1 to {MAX_REQUEST_BYTES} bytes"
        )
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise JsonRequestError("request body must be valid UTF-8") from exc

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise JsonRequestError("request contains a duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise JsonRequestError(f"invalid JSON number: {value}")

    def reject_float(value: str) -> None:
        raise JsonRequestError("floating-point JSON numbers are not accepted")

    try:
        payload = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=reject_float,
        )
    except JsonRequestError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise JsonRequestError("malformed JSON") from exc
    if not isinstance(payload, dict):
        raise JsonRequestError("request body must be a JSON object")
    return payload


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(
        self, subject: str, bucket: str, limit: int, window_seconds: int = 60
    ) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        key = (subject, bucket)
        with self._lock:
            entries = self._entries[key]
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if len(entries) >= limit:
                return False
            entries.append(now)
            return True

    def limited(
        self, subject: str, bucket: str, limit: int, window_seconds: int = 60
    ) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        key = (subject, bucket)
        with self._lock:
            entries = self._entries[key]
            while entries and entries[0] <= cutoff:
                entries.popleft()
            return len(entries) >= limit


class ConcurrentLimiter:
    def __init__(self) -> None:
        self._active: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def acquire(self, subject: str, limit: int) -> bool:
        with self._lock:
            if self._active[subject] >= limit:
                return False
            self._active[subject] += 1
            return True

    def release(self, subject: str) -> None:
        with self._lock:
            remaining = self._active[subject] - 1
            if remaining <= 0:
                self._active.pop(subject, None)
            else:
                self._active[subject] = remaining


class KrabHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], database: Database):
        self._request_slots = threading.BoundedSemaphore(32)
        super().__init__(address, KrabRequestHandler)
        self.database = database
        self.service = Service(database)
        self.limiter = SlidingWindowLimiter()
        self.long_polls = ConcurrentLimiter()

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class KrabRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "imitation-krab"
    sys_version = ""
    server: KrabHTTPServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_OPTIONS(self) -> None:
        self._json_response(
            405, self._error("method_not_allowed", "method not allowed")
        )

    def handle_expect_100(self) -> bool:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return False
        if length > MAX_REQUEST_BYTES:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return False
        return super().handle_expect_100()

    def _dispatch(self, method: str) -> None:
        request_id = secrets.token_hex(8)
        actor: dict[str, Any] | None = None
        status = 500
        try:
            self._validate_headers()
            path, query = self._parse_target()
            if method == "GET" and path == ["v1", "health"]:
                status = 200
                self._json_response(status, {"status": "ok"}, request_id=request_id)
                return

            actor = self._authenticate()
            mutation = method in {"POST", "PATCH"}
            bucket, limit = ("mutation", 30) if mutation else ("read", 120)
            if not self.server.limiter.allow(actor["id"], bucket, limit):
                raise ServiceError(429, "rate_limit", "request rate limit exceeded")

            if method == "GET":
                status, payload = self._route_get(actor, path, query)
                self._json_response(status, payload, request_id=request_id)
                return

            if query:
                raise ServiceError(
                    400,
                    "invalid_query",
                    "mutation endpoints do not accept query parameters",
                )
            body = self._read_json()
            idempotency_key = self.headers.get("Idempotency-Key", "")
            if method == "POST":
                result = self._route_post(actor, path, body, idempotency_key)
            else:
                result = self._route_patch(actor, path, body, idempotency_key)
            status = result.status
            extra = {"Idempotency-Replayed": "true"} if result.replayed else None
            self._json_response(
                result.status,
                result.payload,
                request_id=request_id,
                extra_headers=extra,
            )
        except ServiceError as exc:
            status = exc.status
            if status == 401 or method in {"POST", "PATCH"}:
                self.close_connection = True
            headers = {"Retry-After": "60"} if status == 429 else None
            self._json_response(
                status,
                self._error(exc.code, exc.message),
                request_id=request_id,
                extra_headers=headers,
            )
        except JsonRequestError as exc:
            status = 400
            self._json_response(
                status,
                self._error("invalid_json", str(exc)),
                request_id=request_id,
            )
        except (BrokenPipeError, ConnectionResetError):
            status = 499
        except sqlite3.Error:
            status = 503
            LOG.exception("database failure request_id=%s", request_id)
            self._json_response(
                status,
                self._error("service_unavailable", "database operation failed"),
                request_id=request_id,
            )
        except Exception:
            status = 500
            LOG.exception("unhandled request failure request_id=%s", request_id)
            self._json_response(
                status,
                self._error("internal_error", "internal server error"),
                request_id=request_id,
            )
        finally:
            LOG.info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "method": method,
                        "actor_id": actor["id"] if actor else None,
                        "status": status,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )

    def _route_get(
        self, actor: dict[str, Any], path: list[str], query: dict[str, list[str]]
    ) -> tuple[int, dict[str, Any]]:
        if path == ["v1", "projects"]:
            self._require_query(query, set())
            return 200, self.server.service.list_projects(actor)
        if len(path) == 4 and path[:2] == ["v1", "projects"] and path[3] == "sessions":
            self._require_query(query, set())
            return 200, self.server.service.list_sessions(actor, path[2])
        if (
            len(path) == 6
            and path[:2] == ["v1", "projects"]
            and path[3] == "sessions"
            and path[5] == "queue"
        ):
            self._require_query(query, {"after", "limit", "wait"})
            after = self._query_int(query, "after", 0)
            limit = self._query_int(query, "limit", 50)
            wait = self._query_int(query, "wait", 0)
            acquired = False
            if wait:
                acquired = self.server.long_polls.acquire(
                    actor["id"], MAX_LONG_POLLS_PER_USER
                )
                if not acquired:
                    raise ServiceError(
                        429,
                        "long_poll_limit",
                        "too many concurrent long polls for this user",
                    )
            try:
                return 200, self.server.service.get_queue(
                    actor, path[2], path[4], after=after, limit=limit, wait=wait
                )
            finally:
                if acquired:
                    self.server.long_polls.release(actor["id"])
        if (
            len(path) == 7
            and path[:2] == ["v1", "projects"]
            and path[3] == "sessions"
            and path[5] == "items"
        ):
            self._require_query(query, set())
            return 200, self.server.service.get_item(actor, path[2], path[4], path[6])
        raise ServiceError(404, "not_found", "endpoint not found")

    def _route_post(
        self,
        actor: dict[str, Any],
        path: list[str],
        body: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        if len(path) == 4 and path[:2] == ["v1", "projects"] and path[3] == "sessions":
            return self.server.service.create_session(
                actor, path[2], body, idempotency_key
            )
        if (
            len(path) == 7
            and path[:2] == ["v1", "projects"]
            and path[3] == "sessions"
            and path[5] == "items"
            and path[6] == "close"
        ):
            raise ServiceError(404, "not_found", "endpoint not found")
        if (
            len(path) == 6
            and path[:2] == ["v1", "projects"]
            and path[3] == "sessions"
            and path[5] == "close"
        ):
            return self.server.service.close_session(
                actor, path[2], path[4], body, idempotency_key
            )
        if (
            len(path) == 6
            and path[:2] == ["v1", "projects"]
            and path[3] == "sessions"
            and path[5] == "items"
        ):
            return self.server.service.create_item(
                actor, path[2], path[4], body, idempotency_key
            )
        if (
            len(path) == 8
            and path[:2] == ["v1", "projects"]
            and path[3] == "sessions"
            and path[5] == "items"
            and path[7] == "notify"
        ):
            return self.server.service.remind(
                actor, path[2], path[4], path[6], body, idempotency_key
            )
        raise ServiceError(404, "not_found", "endpoint not found")

    def _route_patch(
        self,
        actor: dict[str, Any],
        path: list[str],
        body: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        if (
            len(path) == 8
            and path[:2] == ["v1", "projects"]
            and path[3] == "sessions"
            and path[5] == "items"
            and path[7] == "status"
        ):
            return self.server.service.change_status(
                actor, path[2], path[4], path[6], body, idempotency_key
            )
        raise ServiceError(404, "not_found", "endpoint not found")

    def _authenticate(self) -> dict[str, Any]:
        peer = self.client_address[0] if self.client_address else "local"
        if self.server.limiter.limited(peer, "auth_failure", 30):
            raise ServiceError(429, "rate_limit", "authentication rate limit exceeded")
        header = self.headers.get("Authorization", "")
        if len(header) > 256 or not header.startswith("Bearer "):
            self._record_auth_failure()
            raise ServiceError(401, "unauthorized", "valid bearer token required")
        token = header[7:]
        actor = self.server.database.authenticate(token)
        if actor is None:
            self._record_auth_failure()
            raise ServiceError(401, "unauthorized", "valid bearer token required")
        return actor

    def _record_auth_failure(self) -> None:
        peer = self.client_address[0] if self.client_address else "local"
        if not self.server.limiter.allow(peer, "auth_failure", 30):
            self.close_connection = True

    def _read_json(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise JsonRequestError("transfer encoding is not accepted")
        content_type = (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            raise JsonRequestError("Content-Type must be application/json")
        content_encoding = (
            self.headers.get("Content-Encoding", "identity").strip().lower()
        )
        if content_encoding not in {"", "identity"}:
            raise JsonRequestError("Content-Encoding is not accepted")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise JsonRequestError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise JsonRequestError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise JsonRequestError(
                f"request body must contain 1 to {MAX_REQUEST_BYTES} bytes"
            )
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise JsonRequestError("incomplete request body")
        return decode_json_object(raw)

    def _parse_target(self) -> tuple[list[str], dict[str, list[str]]]:
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ServiceError(400, "invalid_target", "fragments are not accepted")
        segments: list[str] = []
        try:
            for raw_segment in parsed.path.split("/"):
                if not raw_segment:
                    continue
                segment = unquote(raw_segment, encoding="utf-8", errors="strict")
                if "/" in segment or "\\" in segment or segment in {".", ".."}:
                    raise ServiceError(400, "invalid_target", "invalid path segment")
                segments.append(segment)
            query = parse_qs(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=10,
            )
        except (UnicodeError, ValueError) as exc:
            raise ServiceError(
                400, "invalid_target", "malformed request target"
            ) from exc
        if any(len(values) != 1 for values in query.values()):
            raise ServiceError(
                400, "invalid_query", "query parameters may not be repeated"
            )
        return segments, query

    def _validate_headers(self) -> None:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            raise ServiceError(
                400, "invalid_headers", "exactly one Host header is required"
            )
        host = hosts[0].strip().lower()
        host_name = host.rsplit(":", 1)[0] if ":" in host else host
        if host_name not in {"127.0.0.1", "localhost"}:
            raise ServiceError(
                400, "invalid_headers", "Host must identify the loopback service"
            )
        for name in (
            "Authorization",
            "Content-Length",
            "Idempotency-Key",
            "Transfer-Encoding",
        ):
            if len(self.headers.get_all(name, [])) > 1:
                raise ServiceError(400, "invalid_headers", f"duplicate {name} header")
        if self.headers.get("Transfer-Encoding"):
            raise ServiceError(
                400, "invalid_headers", "transfer encoding is not accepted"
            )
        if self.command == "GET" and self.headers.get("Content-Length") not in {
            None,
            "0",
        }:
            raise ServiceError(
                400, "invalid_headers", "GET request bodies are not accepted"
            )

    @staticmethod
    def _require_query(query: dict[str, list[str]], allowed: set[str]) -> None:
        unknown = query.keys() - allowed
        if unknown:
            raise ServiceError(
                400, "invalid_query", "request contains unknown query parameters"
            )

    @staticmethod
    def _query_int(query: dict[str, list[str]], name: str, default: int) -> int:
        if name not in query:
            return default
        try:
            return int(query[name][0], 10)
        except ValueError as exc:
            raise ServiceError(
                400, "invalid_query", f"{name} must be an integer"
            ) from exc

    def _json_response(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        request_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if request_id:
            self.send_header("X-Request-ID", request_id)
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="imitation-krab"')
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"error": {"code": code, "message": message}}

    def log_message(self, format: str, *args: Any) -> None:
        # The structured request log in _dispatch deliberately omits headers and bodies.
        return


def validate_bind_host(host: str, *, container_bind: bool) -> str:
    if host == DEFAULT_HOST and not container_bind:
        return host
    if host == CONTAINER_HOST and container_bind:
        return host
    if host == CONTAINER_HOST:
        raise ValueError("0.0.0.0 requires the explicit --container-bind option")
    raise ValueError(
        f"v0 only permits {DEFAULT_HOST}, or {CONTAINER_HOST} in container-bind mode"
    )


def make_server(
    database: Database,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    container_bind: bool = False,
) -> KrabHTTPServer:
    validate_bind_host(host, container_bind=container_bind)
    database.initialize()
    return KrabHTTPServer((host, port), database)


def serve(
    database: Database,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    container_bind: bool = False,
) -> None:
    server = make_server(database, host, port, container_bind=container_bind)
    address = server.server_address[0]
    actual_port = server.server_address[1]
    LOG.info("listening on http://%s:%s", address, actual_port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
