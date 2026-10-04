from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import (
    MAX_ACTIVE_SESSIONS_PER_USER,
    MAX_BODY_BYTES,
    MAX_EVENTS_PER_ITEM,
    MAX_LABEL_BYTES,
    MAX_LONG_POLL_SECONDS,
    MAX_NOTE_BYTES,
    MAX_OPEN_ITEMS_PER_ROUTE,
    MAX_QUEUE_LIMIT,
    MAX_TITLE_BYTES,
    MAX_WORK_CLAIMS_PER_PROJECT,
    REMINDER_COOLDOWN_SECONDS,
)
from .db import Database
from .sanitize import (
    ValidationError,
    merge_risk_flags,
    sanitize_text,
    validate_external_id,
    validate_handle,
    validate_idempotency_key,
    validate_object_id,
    validate_project_key,
)

STATUSES = {
    "open.pending",
    "open.in_progress",
    "open.under_review",
    "open.needs_changes",
    "open.approved",
    "closed.approved",
    "closed.rejected",
}

RECIPIENT_TRANSITIONS = {
    ("open.pending", "open.in_progress"),
    ("open.pending", "closed.rejected"),
    ("open.in_progress", "open.pending"),
    ("open.in_progress", "open.under_review"),
    ("open.in_progress", "closed.rejected"),
    ("open.needs_changes", "open.in_progress"),
    ("open.needs_changes", "closed.rejected"),
}

CREATOR_TRANSITIONS = {
    ("open.pending", "closed.rejected"),
    ("open.in_progress", "closed.rejected"),
    ("open.under_review", "open.needs_changes"),
    ("open.under_review", "open.approved"),
    ("open.under_review", "closed.rejected"),
    ("open.needs_changes", "closed.rejected"),
    ("open.approved", "open.needs_changes"),
    ("open.approved", "closed.approved"),
}

WORK_KINDS = {"issue", "pull_request"}
WORK_STATUS_TRANSITIONS = {("claimed", "active"), ("active", "done")}


class ServiceError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MutationResult:
    status: int
    payload: dict[str, Any]
    replayed: bool = False


class Service:
    def __init__(self, database: Database):
        self.db = database

    def list_projects(self, actor: dict[str, Any]) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                """
                SELECT p.id, p.project_key, p.label, pm.role
                FROM project_members pm
                JOIN projects p ON p.id = pm.project_id
                WHERE pm.user_id = ?
                ORDER BY p.project_key
                """,
                (actor["id"],),
            ).fetchall()
        finally:
            conn.close()
        return {
            "projects": [
                {
                    "trusted_metadata": {
                        "project_id": row["id"],
                        "project_key": row["project_key"],
                        "role": row["role"],
                    },
                    "untrusted_text": {"label": row["label"]},
                    "content_risk_flags": list(
                        sanitize_text(
                            row["label"], field="label", max_bytes=MAX_LABEL_BYTES
                        ).risk_flags
                    ),
                }
                for row in rows
            ]
        }

    def list_work_claims(
        self, actor: dict[str, Any], project_key: str, kind: str
    ) -> dict[str, Any]:
        project_key = self._valid_project_key(project_key)
        kind = self._valid_work_kind(kind)
        conn = self.db.connect()
        try:
            project = self._project_membership(conn, actor["id"], project_key)
            rows = conn.execute(
                """
                SELECT
                    wc.*,
                    creator.handle AS creator_handle,
                    assignee.handle AS assignee_handle
                FROM work_claims wc
                JOIN users creator ON creator.id = wc.created_by
                LEFT JOIN users assignee ON assignee.id = wc.assignee_id
                WHERE wc.project_id = ? AND wc.kind = ?
                ORDER BY
                    CASE wc.status
                        WHEN 'active' THEN 0
                        WHEN 'claimed' THEN 1
                        WHEN 'available' THEN 2
                        ELSE 3
                    END,
                    wc.updated_at DESC,
                    wc.id
                """,
                (project["id"], kind),
            ).fetchall()
        finally:
            conn.close()
        return {
            "trusted_metadata": {
                "project_key": project_key,
                "kind": kind,
                "count": len(rows),
            },
            "claims": [
                self._serialize_work_claim(row, project_key) for row in rows
            ],
        }

    def create_work_claim(
        self,
        actor: dict[str, Any],
        project_key: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        project_key = self._valid_project_key(project_key)
        kind = self._valid_work_kind(kind)
        self._require_fields(payload, required={"external_id"})
        try:
            external_id = validate_external_id(payload["external_id"])
        except ValidationError as exc:
            raise self._validation_error(exc) from exc
        key = self._valid_idempotency_key(idempotency_key)
        canonical = {"external_id": external_id}
        operation = f"work_claim.create:{project_key}:{kind}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                duplicate = conn.execute(
                    """
                    SELECT 1 FROM work_claims
                    WHERE project_id = ? AND kind = ? AND external_id = ?
                    """,
                    (project["id"], kind, external_id),
                ).fetchone()
                if duplicate is not None:
                    raise ServiceError(
                        409,
                        "reference_exists",
                        "that external reference is already registered in this queue",
                    )
                claim_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM work_claims WHERE project_id = ?",
                    (project["id"],),
                ).fetchone()["count"]
                if claim_count >= MAX_WORK_CLAIMS_PER_PROJECT:
                    raise ServiceError(
                        429,
                        "claim_limit",
                        "project work-claim limit reached",
                    )
                claim_id = self.db.new_id("clm")
                now = self.db.now()
                conn.execute(
                    """
                    INSERT INTO work_claims(
                        id, project_id, kind, external_id, status, version,
                        created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'available', 1, ?, ?, ?)
                    """,
                    (
                        claim_id,
                        project["id"],
                        kind,
                        external_id,
                        actor["id"],
                        now,
                        now,
                    ),
                )
                self.db._audit(
                    conn,
                    actor["id"],
                    "work_claim.created",
                    claim_id,
                    {
                        "project_id": project["id"],
                        "kind": kind,
                        "external_id": external_id,
                    },
                )
                row = self._work_claim_row(
                    conn, project["id"], kind, claim_id
                )
                return 201, self._serialize_work_claim(row, project_key)

            result = self._idempotent(
                conn, actor["id"], key, operation, canonical, mutate
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_work(
        self,
        actor: dict[str, Any],
        project_key: str,
        kind: str,
        claim_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        project_key = self._valid_project_key(project_key)
        kind = self._valid_work_kind(kind)
        claim_id = self._valid_object_id(claim_id, "clm")
        self._require_fields(payload, required={"expected_version"})
        version = self._valid_expected_version(payload["expected_version"])
        key = self._valid_idempotency_key(idempotency_key)
        canonical = {"expected_version": version}
        operation = f"work_claim.claim:{project_key}:{kind}:{claim_id}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                claim = self._work_claim_row(
                    conn, project["id"], kind, claim_id
                )
                self._require_claim_version(claim, version)
                if claim["status"] != "available":
                    raise ServiceError(
                        409, "already_claimed", "work is not available to claim"
                    )
                now = self.db.now()
                cursor = conn.execute(
                    """
                    UPDATE work_claims
                    SET assignee_id = ?, status = 'claimed',
                        version = version + 1, updated_at = ?
                    WHERE id = ? AND project_id = ? AND kind = ?
                      AND status = 'available' AND assignee_id IS NULL AND version = ?
                    """,
                    (
                        actor["id"],
                        now,
                        claim_id,
                        project["id"],
                        kind,
                        version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ServiceError(
                        409, "version_conflict", "work-claim version has changed"
                    )
                self.db._audit(
                    conn,
                    actor["id"],
                    "work_claim.claimed",
                    claim_id,
                    {"project_id": project["id"], "kind": kind},
                )
                updated = self._work_claim_row(
                    conn, project["id"], kind, claim_id
                )
                return 200, self._serialize_work_claim(updated, project_key)

            result = self._idempotent(
                conn, actor["id"], key, operation, canonical, mutate
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_work(
        self,
        actor: dict[str, Any],
        project_key: str,
        kind: str,
        claim_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        project_key = self._valid_project_key(project_key)
        kind = self._valid_work_kind(kind)
        claim_id = self._valid_object_id(claim_id, "clm")
        self._require_fields(payload, required={"expected_version"})
        version = self._valid_expected_version(payload["expected_version"])
        key = self._valid_idempotency_key(idempotency_key)
        canonical = {"expected_version": version}
        operation = f"work_claim.release:{project_key}:{kind}:{claim_id}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                claim = self._work_claim_row(
                    conn, project["id"], kind, claim_id
                )
                self._require_claim_version(claim, version)
                if claim["status"] not in {"claimed", "active"}:
                    raise ServiceError(
                        409,
                        "invalid_transition",
                        "only claimed or active work can be released",
                    )
                if (
                    claim["assignee_id"] != actor["id"]
                    and project["role"] != "admin"
                ):
                    raise ServiceError(
                        403,
                        "forbidden",
                        "only the assignee or a project admin may release work",
                    )
                previous_assignee = claim["assignee_id"]
                now = self.db.now()
                cursor = conn.execute(
                    """
                    UPDATE work_claims
                    SET assignee_id = NULL, status = 'available',
                        version = version + 1, updated_at = ?
                    WHERE id = ? AND project_id = ? AND kind = ? AND version = ?
                      AND status IN ('claimed', 'active')
                    """,
                    (now, claim_id, project["id"], kind, version),
                )
                if cursor.rowcount != 1:
                    raise ServiceError(
                        409, "version_conflict", "work-claim version has changed"
                    )
                self.db._audit(
                    conn,
                    actor["id"],
                    "work_claim.released",
                    claim_id,
                    {
                        "project_id": project["id"],
                        "kind": kind,
                        "previous_assignee_id": previous_assignee,
                    },
                )
                updated = self._work_claim_row(
                    conn, project["id"], kind, claim_id
                )
                return 200, self._serialize_work_claim(updated, project_key)

            result = self._idempotent(
                conn, actor["id"], key, operation, canonical, mutate
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def change_work_status(
        self,
        actor: dict[str, Any],
        project_key: str,
        kind: str,
        claim_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        project_key = self._valid_project_key(project_key)
        kind = self._valid_work_kind(kind)
        claim_id = self._valid_object_id(claim_id, "clm")
        self._require_fields(payload, required={"status", "expected_version"})
        status = payload["status"]
        if status not in {"active", "done"}:
            raise ServiceError(
                400, "invalid_status", "status must be active or done"
            )
        version = self._valid_expected_version(payload["expected_version"])
        key = self._valid_idempotency_key(idempotency_key)
        canonical = {"status": status, "expected_version": version}
        operation = f"work_claim.status:{project_key}:{kind}:{claim_id}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                claim = self._work_claim_row(
                    conn, project["id"], kind, claim_id
                )
                self._require_claim_version(claim, version)
                if claim["assignee_id"] != actor["id"]:
                    raise ServiceError(
                        403,
                        "forbidden",
                        "only the assignee may advance claimed work",
                    )
                if (claim["status"], status) not in WORK_STATUS_TRANSITIONS:
                    raise ServiceError(
                        409,
                        "invalid_transition",
                        "work-claim status transition is not permitted",
                    )
                now = self.db.now()
                cursor = conn.execute(
                    """
                    UPDATE work_claims
                    SET status = ?, version = version + 1, updated_at = ?
                    WHERE id = ? AND project_id = ? AND kind = ? AND version = ?
                    """,
                    (status, now, claim_id, project["id"], kind, version),
                )
                if cursor.rowcount != 1:
                    raise ServiceError(
                        409, "version_conflict", "work-claim version has changed"
                    )
                self.db._audit(
                    conn,
                    actor["id"],
                    "work_claim.status_changed",
                    claim_id,
                    {
                        "project_id": project["id"],
                        "kind": kind,
                        "from_status": claim["status"],
                        "to_status": status,
                    },
                )
                updated = self._work_claim_row(
                    conn, project["id"], kind, claim_id
                )
                return 200, self._serialize_work_claim(updated, project_key)

            result = self._idempotent(
                conn, actor["id"], key, operation, canonical, mutate
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_session(
        self,
        actor: dict[str, Any],
        project_key: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        project_key = self._valid_project_key(project_key)
        self._require_fields(payload, required={"label", "participants"})
        label = self._sanitized_text(
            payload["label"], field="label", max_bytes=MAX_LABEL_BYTES
        )
        participants = payload["participants"]
        if (
            not isinstance(participants, list)
            or not participants
            or len(participants) > 32
        ):
            raise ServiceError(
                400, "invalid_participants", "participants must contain 1 to 32 handles"
            )
        try:
            handles = sorted(
                {validate_handle(value) for value in participants} | {actor["handle"]}
            )
            key = validate_idempotency_key(idempotency_key)
        except ValidationError as exc:
            raise self._validation_error(exc) from exc

        canonical = {"label": label.text, "participants": handles}
        operation = f"session.create:{project_key}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                active_sessions = conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM sessions
                    WHERE project_id = ? AND created_by = ? AND state = 'active'
                    """,
                    (project["id"], actor["id"]),
                ).fetchone()["count"]
                if active_sessions >= MAX_ACTIVE_SESSIONS_PER_USER:
                    raise ServiceError(
                        429,
                        "session_limit",
                        "too many active sessions created by this user in the project",
                    )
                rows = []
                for handle in handles:
                    row = conn.execute(
                        """
                    SELECT u.id, u.handle
                    FROM users u
                    JOIN project_members pm ON pm.user_id = u.id
                    WHERE pm.project_id = ? AND u.active = 1 AND u.handle = ?
                    """,
                        (project["id"], handle),
                    ).fetchone()
                    if row is not None:
                        rows.append(row)
                if {row["handle"] for row in rows} != set(handles):
                    raise ServiceError(
                        400,
                        "invalid_participants",
                        "all participants must be active project members",
                    )
                session_id = self.db.new_id("ses")
                now = self.db.now()
                conn.execute(
                    """
                    INSERT INTO sessions(id, project_id, label, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_id, project["id"], label.text, actor["id"], now),
                )
                conn.executemany(
                    """
                    INSERT INTO session_members(project_id, session_id, user_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [(project["id"], session_id, row["id"], now) for row in rows],
                )
                self.db._audit(
                    conn,
                    actor["id"],
                    "session.created",
                    session_id,
                    {"project_id": project["id"], "participant_count": len(rows)},
                )
                return 201, {
                    "trusted_metadata": {
                        "session_id": session_id,
                        "project_id": project["id"],
                        "project_key": project_key,
                        "state": "active",
                        "created_by": actor["id"],
                        "participants": [
                            {"user_id": row["id"], "handle": row["handle"]}
                            for row in sorted(rows, key=lambda item: item["handle"])
                        ],
                        "created_at": now,
                    },
                    "untrusted_text": {"label": label.text},
                    "content_risk_flags": list(label.risk_flags),
                }

            result = self._idempotent(
                conn, actor["id"], key, operation, canonical, mutate
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_sessions(self, actor: dict[str, Any], project_key: str) -> dict[str, Any]:
        project_key = self._valid_project_key(project_key)
        conn = self.db.connect()
        try:
            project = self._project_membership(conn, actor["id"], project_key)
            rows = conn.execute(
                """
                SELECT s.id, s.label, s.state, s.created_by, s.created_at, s.closed_at
                FROM sessions s
                JOIN session_members sm
                  ON sm.project_id = s.project_id AND sm.session_id = s.id
                WHERE s.project_id = ? AND sm.user_id = ?
                ORDER BY s.created_at DESC
                """,
                (project["id"], actor["id"]),
            ).fetchall()
        finally:
            conn.close()
        sessions = []
        for row in rows:
            label = sanitize_text(
                row["label"], field="label", max_bytes=MAX_LABEL_BYTES
            )
            sessions.append(
                {
                    "trusted_metadata": {
                        "session_id": row["id"],
                        "project_key": project_key,
                        "state": row["state"],
                        "created_by": row["created_by"],
                        "created_at": row["created_at"],
                        "closed_at": row["closed_at"],
                    },
                    "untrusted_text": {"label": label.text},
                    "content_risk_flags": list(label.risk_flags),
                }
            )
        return {"sessions": sessions}

    def close_session(
        self,
        actor: dict[str, Any],
        project_key: str,
        session_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        project_key = self._valid_project_key(project_key)
        session_id = self._valid_object_id(session_id, "ses")
        self._require_fields(payload, required=set())
        key = self._valid_idempotency_key(idempotency_key)
        operation = f"session.close:{project_key}:{session_id}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                session = self._session_membership(
                    conn, actor["id"], project["id"], session_id
                )
                if session["state"] == "closed":
                    raise ServiceError(
                        409, "session_closed", "session is already closed"
                    )
                if session["created_by"] != actor["id"] and project["role"] != "admin":
                    raise ServiceError(
                        403,
                        "forbidden",
                        "only the session creator or project admin may close it",
                    )
                open_count = conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM items
                    WHERE project_id = ? AND session_id = ? AND status LIKE 'open.%'
                    """,
                    (project["id"], session_id),
                ).fetchone()["count"]
                if open_count:
                    raise ServiceError(
                        409, "open_items", "session contains non-terminal work items"
                    )
                now = self.db.now()
                conn.execute(
                    "UPDATE sessions SET state = 'closed', closed_at = ? WHERE id = ?",
                    (now, session_id),
                )
                self.db._audit(
                    conn,
                    actor["id"],
                    "session.closed",
                    session_id,
                    {"project_id": project["id"]},
                )
                return 200, {
                    "trusted_metadata": {
                        "session_id": session_id,
                        "project_key": project_key,
                        "state": "closed",
                        "closed_at": now,
                    }
                }

            result = self._idempotent(
                conn, actor["id"], key, operation, payload, mutate
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_item(
        self,
        actor: dict[str, Any],
        project_key: str,
        session_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        project_key = self._valid_project_key(project_key)
        session_id = self._valid_object_id(session_id, "ses")
        self._require_fields(
            payload, required={"to", "title", "body"}, optional={"claim_id"}
        )
        try:
            recipient_handle = validate_handle(payload["to"])
        except ValidationError as exc:
            raise self._validation_error(exc) from exc
        claim_id = None
        if "claim_id" in payload:
            claim_id = self._valid_object_id(payload["claim_id"], "clm")
        title = self._sanitized_text(
            payload["title"], field="title", max_bytes=MAX_TITLE_BYTES
        )
        body = self._sanitized_text(
            payload["body"], field="body", max_bytes=MAX_BODY_BYTES
        )
        flags = merge_risk_flags(title.risk_flags, body.risk_flags)
        key = self._valid_idempotency_key(idempotency_key)
        canonical = {
            "to": recipient_handle,
            "title": title.text,
            "body": body.text,
            "claim_id": claim_id,
        }
        operation = f"item.create:{project_key}:{session_id}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                self._session_membership(conn, actor["id"], project["id"], session_id)
                if claim_id is not None:
                    linked_claim = conn.execute(
                        """
                        SELECT 1 FROM work_claims
                        WHERE id = ? AND project_id = ?
                        """,
                        (claim_id, project["id"]),
                    ).fetchone()
                    if linked_claim is None:
                        raise ServiceError(
                            404,
                            "work_claim_not_found",
                            "work claim not found in this project",
                        )
                session = conn.execute(
                    "SELECT state FROM sessions WHERE id = ? AND project_id = ?",
                    (session_id, project["id"]),
                ).fetchone()
                if session["state"] != "active":
                    raise ServiceError(
                        409, "session_closed", "session does not accept new work"
                    )
                recipient = conn.execute(
                    """
                    SELECT u.id, u.handle
                    FROM users u
                    JOIN session_members sm ON sm.user_id = u.id
                    WHERE u.handle = ? AND u.active = 1
                      AND sm.project_id = ? AND sm.session_id = ?
                    """,
                    (recipient_handle, project["id"], session_id),
                ).fetchone()
                if recipient is None:
                    raise ServiceError(
                        404,
                        "recipient_not_found",
                        "recipient is not an active session participant",
                    )
                if recipient["id"] == actor["id"]:
                    raise ServiceError(
                        400, "self_assignment", "recipient must be another user"
                    )
                open_count = conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM items
                    WHERE project_id = ? AND session_id = ?
                      AND creator_id = ? AND recipient_id = ? AND status LIKE 'open.%'
                    """,
                    (project["id"], session_id, actor["id"], recipient["id"]),
                ).fetchone()["count"]
                if open_count >= MAX_OPEN_ITEMS_PER_ROUTE:
                    raise ServiceError(
                        429, "queue_limit", "too many open items for this recipient"
                    )

                item_id = self.db.new_id("itm")
                now = self.db.now()
                conn.execute(
                    """
                    INSERT INTO items(
                        id, project_id, session_id, creator_id, recipient_id, work_claim_id,
                        title, body, risk_flags, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open.pending', ?, ?)
                    """,
                    (
                        item_id,
                        project["id"],
                        session_id,
                        actor["id"],
                        recipient["id"],
                        claim_id,
                        title.text,
                        body.text,
                        self._json_list(flags),
                        now,
                        now,
                    ),
                )
                event_seq = self._insert_event(
                    conn,
                    project["id"],
                    session_id,
                    item_id,
                    actor["id"],
                    "item.created",
                    None,
                    "open.pending",
                    None,
                    flags,
                    recipient["id"],
                )
                row = self._item_row(
                    conn, project["id"], session_id, item_id, actor["id"]
                )
                response = self._serialize_item(row)
                response["trusted_metadata"]["event_seq"] = event_seq
                return 201, response

            result = self._idempotent(
                conn, actor["id"], key, operation, canonical, mutate
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_item(
        self, actor: dict[str, Any], project_key: str, session_id: str, item_id: str
    ) -> dict[str, Any]:
        project_key = self._valid_project_key(project_key)
        session_id = self._valid_object_id(session_id, "ses")
        item_id = self._valid_object_id(item_id, "itm")
        conn = self.db.connect()
        try:
            project = self._project_membership(conn, actor["id"], project_key)
            self._session_membership(conn, actor["id"], project["id"], session_id)
            row = self._item_row(conn, project["id"], session_id, item_id, actor["id"])
            events = conn.execute(
                """
                SELECT e.*, u.handle AS actor_handle
                FROM events e JOIN users u ON u.id = e.actor_id
                WHERE e.project_id = ? AND e.session_id = ? AND e.item_id = ?
                ORDER BY e.seq
                """,
                (project["id"], session_id, item_id),
            ).fetchall()
        finally:
            conn.close()
        response = self._serialize_item(row)
        response["history"] = [self._serialize_history_event(event) for event in events]
        return response

    def change_status(
        self,
        actor: dict[str, Any],
        project_key: str,
        session_id: str,
        item_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        project_key = self._valid_project_key(project_key)
        session_id = self._valid_object_id(session_id, "ses")
        item_id = self._valid_object_id(item_id, "itm")
        self._require_fields(
            payload, required={"status", "expected_version"}, optional={"note"}
        )
        status = payload["status"]
        version = payload["expected_version"]
        if status not in STATUSES:
            raise ServiceError(400, "invalid_status", "unknown status")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ServiceError(
                400, "invalid_version", "expected_version must be a positive integer"
            )
        note_value = payload.get("note")
        note = None
        note_flags: tuple[str, ...] = ()
        if note_value is not None:
            cleaned = self._sanitized_text(
                note_value, field="note", max_bytes=MAX_NOTE_BYTES, allow_empty=False
            )
            note, note_flags = cleaned.text, cleaned.risk_flags
        if status in {"open.needs_changes", "closed.rejected"} and note is None:
            raise ServiceError(400, "note_required", "this status requires a note")
        key = self._valid_idempotency_key(idempotency_key)
        canonical = {"status": status, "expected_version": version, "note": note}
        operation = f"item.status:{project_key}:{session_id}:{item_id}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                self._session_membership(conn, actor["id"], project["id"], session_id)
                item = self._item_row(
                    conn, project["id"], session_id, item_id, actor["id"]
                )
                if item["version"] != version:
                    raise ServiceError(
                        409, "version_conflict", "work item version has changed"
                    )
                transition = (item["status"], status)
                if actor["id"] == item["recipient_id"]:
                    allowed = transition in RECIPIENT_TRANSITIONS
                    notify_user = item["creator_id"]
                else:
                    allowed = transition in CREATOR_TRANSITIONS
                    notify_user = item["recipient_id"]
                if not allowed:
                    raise ServiceError(
                        409,
                        "invalid_transition",
                        "status transition is not permitted for this actor",
                    )
                self._enforce_event_limit(
                    conn,
                    item_id,
                    allow_terminal=status.startswith("closed."),
                )
                now = self.db.now()
                cursor = conn.execute(
                    """
                    UPDATE items
                    SET status = ?, version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ?
                    """,
                    (status, now, item_id, version),
                )
                if cursor.rowcount != 1:
                    raise ServiceError(
                        409, "version_conflict", "work item version has changed"
                    )
                event_seq = self._insert_event(
                    conn,
                    project["id"],
                    session_id,
                    item_id,
                    actor["id"],
                    "status.changed",
                    item["status"],
                    status,
                    note,
                    note_flags,
                    notify_user,
                )
                updated = self._item_row(
                    conn, project["id"], session_id, item_id, actor["id"]
                )
                response = self._serialize_item(updated)
                response["trusted_metadata"]["event_seq"] = event_seq
                return 200, response

            result = self._idempotent(
                conn, actor["id"], key, operation, canonical, mutate
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def remind(
        self,
        actor: dict[str, Any],
        project_key: str,
        session_id: str,
        item_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        project_key = self._valid_project_key(project_key)
        session_id = self._valid_object_id(session_id, "ses")
        item_id = self._valid_object_id(item_id, "itm")
        self._require_fields(payload, required=set())
        key = self._valid_idempotency_key(idempotency_key)
        operation = f"item.remind:{project_key}:{session_id}:{item_id}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                self._session_membership(conn, actor["id"], project["id"], session_id)
                item = self._item_row(
                    conn, project["id"], session_id, item_id, actor["id"]
                )
                if item["status"].startswith("closed."):
                    raise ServiceError(
                        409, "item_closed", "closed work items cannot be reminded"
                    )
                self._enforce_event_limit(conn, item_id, allow_terminal=False)
                threshold = (
                    (datetime.now(UTC) - timedelta(seconds=REMINDER_COOLDOWN_SECONDS))
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )
                recent = conn.execute(
                    """
                    SELECT 1 FROM events
                    WHERE item_id = ? AND actor_id = ? AND kind = 'item.reminded'
                      AND created_at > ?
                    LIMIT 1
                    """,
                    (item_id, actor["id"], threshold),
                ).fetchone()
                if recent:
                    raise ServiceError(
                        429, "reminder_rate_limit", "reminder cooldown is active"
                    )
                notify_user = (
                    item["recipient_id"]
                    if actor["id"] == item["creator_id"]
                    else item["creator_id"]
                )
                event_seq = self._insert_event(
                    conn,
                    project["id"],
                    session_id,
                    item_id,
                    actor["id"],
                    "item.reminded",
                    item["status"],
                    item["status"],
                    None,
                    (),
                    notify_user,
                )
                return 202, {
                    "trusted_metadata": {
                        "event_seq": event_seq,
                        "kind": "item.reminded",
                        "project_key": project_key,
                        "session_id": session_id,
                        "item_id": item_id,
                        "recipient_id": notify_user,
                    }
                }

            result = self._idempotent(
                conn, actor["id"], key, operation, payload, mutate
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_queue(
        self,
        actor: dict[str, Any],
        project_key: str,
        session_id: str,
        *,
        after: int = 0,
        limit: int = 50,
        wait: int = 0,
    ) -> dict[str, Any]:
        project_key = self._valid_project_key(project_key)
        session_id = self._valid_object_id(session_id, "ses")
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ServiceError(
                400, "invalid_cursor", "after must be a non-negative integer"
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_QUEUE_LIMIT
        ):
            raise ServiceError(
                400, "invalid_limit", f"limit must be between 1 and {MAX_QUEUE_LIMIT}"
            )
        if (
            isinstance(wait, bool)
            or not isinstance(wait, int)
            or not 0 <= wait <= MAX_LONG_POLL_SECONDS
        ):
            raise ServiceError(
                400,
                "invalid_wait",
                f"wait must be between 0 and {MAX_LONG_POLL_SECONDS}",
            )

        deadline = time.monotonic() + wait
        while True:
            conn = self.db.connect()
            try:
                project = self._project_membership(conn, actor["id"], project_key)
                self._session_membership(conn, actor["id"], project["id"], session_id)
                rows = conn.execute(
                    """
                    SELECT
                        e.*, i.title, i.body, i.status AS current_status, i.version,
                        i.work_claim_id,
                        wc.kind AS work_claim_kind,
                        wc.external_id AS work_claim_external_id,
                        creator.id AS creator_id, creator.handle AS creator_handle,
                        recipient.id AS recipient_id, recipient.handle AS recipient_handle,
                        actor.handle AS actor_handle
                    FROM deliveries d
                    JOIN events e ON e.seq = d.event_seq
                    JOIN items i ON i.id = e.item_id
                    JOIN users creator ON creator.id = i.creator_id
                    JOIN users recipient ON recipient.id = i.recipient_id
                    JOIN users actor ON actor.id = e.actor_id
                    LEFT JOIN work_claims wc ON wc.id = i.work_claim_id
                    WHERE d.user_id = ? AND e.project_id = ? AND e.session_id = ?
                      AND e.seq > ?
                    ORDER BY e.seq
                    LIMIT ?
                    """,
                    (actor["id"], project["id"], session_id, after, limit),
                ).fetchall()
            finally:
                conn.close()
            if rows or time.monotonic() >= deadline:
                break
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

        events = [self._serialize_queue_event(row, project_key) for row in rows]
        return {
            "trusted_metadata": {
                "project_key": project_key,
                "session_id": session_id,
                "after": after,
                "next_cursor": rows[-1]["seq"] if rows else after,
            },
            "events": events,
        }

    def _project_membership(
        self, conn: sqlite3.Connection, user_id: str, project_key: str
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT p.id, p.project_key, pm.role
            FROM projects p
            JOIN project_members pm ON pm.project_id = p.id
            JOIN users u ON u.id = pm.user_id
            WHERE p.project_key = ? AND pm.user_id = ? AND u.active = 1
            """,
            (project_key, user_id),
        ).fetchone()
        if row is None:
            raise ServiceError(404, "scope_not_found", "project scope not found")
        return row

    def _session_membership(
        self, conn: sqlite3.Connection, user_id: str, project_id: str, session_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT s.id, s.state, s.created_by
            FROM sessions s
            JOIN session_members sm
              ON sm.project_id = s.project_id AND sm.session_id = s.id
            WHERE s.project_id = ? AND s.id = ? AND sm.user_id = ?
            """,
            (project_id, session_id, user_id),
        ).fetchone()
        if row is None:
            raise ServiceError(404, "scope_not_found", "session scope not found")
        return row

    @staticmethod
    def _work_claim_row(
        conn: sqlite3.Connection, project_id: str, kind: str, claim_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT
                wc.*,
                creator.handle AS creator_handle,
                assignee.handle AS assignee_handle
            FROM work_claims wc
            JOIN users creator ON creator.id = wc.created_by
            LEFT JOIN users assignee ON assignee.id = wc.assignee_id
            WHERE wc.project_id = ? AND wc.kind = ? AND wc.id = ?
            """,
            (project_id, kind, claim_id),
        ).fetchone()
        if row is None:
            raise ServiceError(404, "work_claim_not_found", "work claim not found")
        return row

    @staticmethod
    def _require_claim_version(claim: sqlite3.Row, expected_version: int) -> None:
        if claim["version"] != expected_version:
            raise ServiceError(
                409, "version_conflict", "work-claim version has changed"
            )

    def _item_row(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        session_id: str,
        item_id: str,
        actor_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT
                i.*, p.project_key,
                creator.handle AS creator_handle,
                recipient.handle AS recipient_handle,
                wc.kind AS work_claim_kind,
                wc.external_id AS work_claim_external_id
            FROM items i
            JOIN projects p ON p.id = i.project_id
            JOIN users creator ON creator.id = i.creator_id
            JOIN users recipient ON recipient.id = i.recipient_id
            LEFT JOIN work_claims wc ON wc.id = i.work_claim_id
            WHERE i.project_id = ? AND i.session_id = ? AND i.id = ?
              AND (i.creator_id = ? OR i.recipient_id = ?)
            """,
            (project_id, session_id, item_id, actor_id, actor_id),
        ).fetchone()
        if row is None:
            raise ServiceError(404, "item_not_found", "work item not found")
        return row

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        session_id: str,
        item_id: str,
        actor_id: str,
        kind: str,
        from_status: str | None,
        to_status: str | None,
        note: str | None,
        risk_flags: tuple[str, ...],
        delivery_user_id: str,
    ) -> int:
        now = self.db.now()
        cursor = conn.execute(
            """
            INSERT INTO events(
                project_id, session_id, item_id, actor_id, kind,
                from_status, to_status, note, risk_flags, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                session_id,
                item_id,
                actor_id,
                kind,
                from_status,
                to_status,
                note,
                self._json_list(risk_flags),
                now,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an event sequence")
        event_seq = cursor.lastrowid
        conn.execute(
            "INSERT INTO deliveries(event_seq, user_id, created_at) VALUES (?, ?, ?)",
            (event_seq, delivery_user_id, now),
        )
        return event_seq

    @staticmethod
    def _enforce_event_limit(
        conn: sqlite3.Connection, item_id: str, *, allow_terminal: bool
    ) -> None:
        event_count = conn.execute(
            "SELECT COUNT(*) AS count FROM events WHERE item_id = ?", (item_id,)
        ).fetchone()["count"]
        if event_count >= MAX_EVENTS_PER_ITEM and not allow_terminal:
            raise ServiceError(429, "event_limit", "work item event limit reached")

    def _idempotent(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        key: str,
        operation: str,
        canonical_request: dict[str, Any],
        mutate: Callable[[], tuple[int, dict[str, Any]]],
    ) -> MutationResult:
        request_json = json.dumps(
            canonical_request, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        existing = conn.execute(
            """
            SELECT operation, request_hash, status_code, response_json
            FROM idempotency_keys WHERE user_id = ? AND key = ?
            """,
            (user_id, key),
        ).fetchone()
        if existing is not None:
            if (
                existing["operation"] != operation
                or existing["request_hash"] != request_hash
            ):
                raise ServiceError(
                    409,
                    "idempotency_conflict",
                    "idempotency key was used for another request",
                )
            return MutationResult(
                existing["status_code"],
                json.loads(existing["response_json"]),
                replayed=True,
            )
        status_code, response = mutate()
        response_json = json.dumps(
            response, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        conn.execute(
            """
            INSERT INTO idempotency_keys(
                user_id, key, operation, request_hash, status_code, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                key,
                operation,
                request_hash,
                status_code,
                response_json,
                self.db.now(),
            ),
        )
        return MutationResult(status_code, response)

    @staticmethod
    def _serialize_work_claim(
        row: sqlite3.Row, project_key: str
    ) -> dict[str, Any]:
        assignee = None
        if row["assignee_id"] is not None:
            assignee = {
                "user_id": row["assignee_id"],
                "handle": row["assignee_handle"],
            }
        return {
            "trusted_metadata": {
                "claim_id": row["id"],
                "project_key": project_key,
                "kind": row["kind"],
                "external_id": row["external_id"],
                "status": row["status"],
                "version": row["version"],
                "assignee": assignee,
                "created_by": {
                    "user_id": row["created_by"],
                    "handle": row["creator_handle"],
                },
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        }

    @staticmethod
    def _linked_work_metadata(row: sqlite3.Row) -> dict[str, Any] | None:
        if row["work_claim_id"] is None:
            return None
        return {
            "claim_id": row["work_claim_id"],
            "kind": row["work_claim_kind"],
            "external_id": row["work_claim_external_id"],
        }

    @staticmethod
    def _serialize_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "trusted_metadata": {
                "item_id": row["id"],
                "project_key": row["project_key"],
                "session_id": row["session_id"],
                "creator": {
                    "user_id": row["creator_id"],
                    "handle": row["creator_handle"],
                },
                "recipient": {
                    "user_id": row["recipient_id"],
                    "handle": row["recipient_handle"],
                },
                "work_claim": Service._linked_work_metadata(row),
                "status": row["status"],
                "version": row["version"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
            "untrusted_text": {"title": row["title"], "body": row["body"]},
            "content_risk_flags": json.loads(row["risk_flags"]),
        }

    @staticmethod
    def _serialize_history_event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "trusted_metadata": {
                "event_seq": row["seq"],
                "kind": row["kind"],
                "actor": {"user_id": row["actor_id"], "handle": row["actor_handle"]},
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "created_at": row["created_at"],
            },
            "untrusted_text": {"note": row["note"]},
            "content_risk_flags": json.loads(row["risk_flags"]),
        }

    @staticmethod
    def _serialize_queue_event(row: sqlite3.Row, project_key: str) -> dict[str, Any]:
        text: dict[str, Any] = {"title": row["title"]}
        if row["kind"] == "item.created":
            text["body"] = row["body"]
        if row["note"] is not None:
            text["note"] = row["note"]
        return {
            "trusted_metadata": {
                "event_seq": row["seq"],
                "kind": row["kind"],
                "project_key": project_key,
                "session_id": row["session_id"],
                "item_id": row["item_id"],
                "actor": {"user_id": row["actor_id"], "handle": row["actor_handle"]},
                "creator": {
                    "user_id": row["creator_id"],
                    "handle": row["creator_handle"],
                },
                "recipient": {
                    "user_id": row["recipient_id"],
                    "handle": row["recipient_handle"],
                },
                "work_claim": Service._linked_work_metadata(row),
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "current_status": row["current_status"],
                "version": row["version"],
                "created_at": row["created_at"],
            },
            "untrusted_text": text,
            "content_risk_flags": json.loads(row["risk_flags"]),
        }

    @staticmethod
    def _json_list(values: tuple[str, ...]) -> str:
        return json.dumps(list(values), separators=(",", ":"))

    @staticmethod
    def _require_fields(
        payload: dict[str, Any], *, required: set[str], optional: set[str] | None = None
    ) -> None:
        if not isinstance(payload, dict):
            raise ServiceError(400, "invalid_request", "request body must be an object")
        optional = optional or set()
        missing = required - payload.keys()
        unknown = payload.keys() - required - optional
        if missing:
            raise ServiceError(
                400,
                "missing_fields",
                f"missing required fields: {', '.join(sorted(missing))}",
            )
        if unknown:
            raise ServiceError(400, "unknown_fields", "request contains unknown fields")

    @staticmethod
    def _validation_error(exc: ValidationError) -> ServiceError:
        return ServiceError(400, "validation_error", str(exc))

    def _valid_project_key(self, value: object) -> str:
        try:
            return validate_project_key(value)
        except ValidationError as exc:
            raise self._validation_error(exc) from exc

    @staticmethod
    def _valid_work_kind(value: object) -> str:
        if not isinstance(value, str) or value not in WORK_KINDS:
            raise ServiceError(400, "invalid_kind", "unknown work-claim kind")
        return value

    @staticmethod
    def _valid_expected_version(value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ServiceError(
                400, "invalid_version", "expected_version must be a positive integer"
            )
        return value

    def _valid_object_id(self, value: object, prefix: str) -> str:
        try:
            return validate_object_id(value, prefix)
        except ValidationError as exc:
            raise self._validation_error(exc) from exc

    def _valid_idempotency_key(self, value: object) -> str:
        try:
            return validate_idempotency_key(value)
        except ValidationError as exc:
            raise self._validation_error(exc) from exc

    def _sanitized_text(self, value: object, **kwargs: Any):
        try:
            return sanitize_text(value, **kwargs)
        except ValidationError as exc:
            raise self._validation_error(exc) from exc
