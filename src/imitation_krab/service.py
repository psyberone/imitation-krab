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
    MAX_CLAIM_IMPORT_ENTRIES,
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
WORK_STATUSES = {"available", "claimed", "active", "under_review", "done"}
WORK_STATUS_TARGETS = {"active", "under_review", "done"}
COORDINATION_ROLES = {"coordinator", "admin"}


class ServiceError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


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
                SELECT
                    p.id,
                    p.project_key,
                    p.label,
                    CASE
                        WHEN pm.role = 'admin' THEN 'admin'
                        WHEN pc.user_id IS NOT NULL THEN 'coordinator'
                        ELSE 'member'
                    END AS role
                FROM project_members pm
                JOIN projects p ON p.id = pm.project_id
                LEFT JOIN project_coordinators pc
                  ON pc.project_id = pm.project_id AND pc.user_id = pm.user_id
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
        self,
        actor: dict[str, Any],
        project_key: str,
        kind: str,
        *,
        status: str | None = None,
        assignee: str | None = None,
    ) -> dict[str, Any]:
        project_key = self._valid_project_key(project_key)
        kind = self._valid_work_kind(kind)
        if status is not None:
            status = self._valid_work_status(status)
        if assignee is not None:
            assignee = self._valid_handle(assignee)
        conn = self.db.connect()
        try:
            project = self._project_membership(conn, actor["id"], project_key)
            rows = conn.execute(
                """
                SELECT
                    wc.*,
                    CASE
                        WHEN review.claim_id IS NOT NULL THEN 'under_review'
                        ELSE wc.status
                    END AS effective_status,
                    review.submitted_by AS review_submitted_by,
                    review.submitted_at AS review_submitted_at,
                    creator.handle AS creator_handle,
                    assignee.handle AS assignee_handle
                FROM work_claims wc
                LEFT JOIN work_claim_review_requests review
                  ON review.project_id = wc.project_id AND review.claim_id = wc.id
                JOIN users creator ON creator.id = wc.created_by
                LEFT JOIN users assignee ON assignee.id = wc.assignee_id
                WHERE wc.project_id = ? AND wc.kind = ?
                  AND (
                    ? IS NULL OR
                    CASE
                        WHEN review.claim_id IS NOT NULL THEN 'under_review'
                        ELSE wc.status
                    END = ?
                  )
                  AND (? IS NULL OR assignee.handle = ?)
                ORDER BY
                    CASE
                        WHEN review.claim_id IS NOT NULL THEN 0
                        WHEN wc.status = 'active' THEN 1
                        WHEN wc.status = 'claimed' THEN 2
                        WHEN wc.status = 'available' THEN 3
                        ELSE 4
                    END,
                    wc.updated_at DESC,
                    wc.id
                """,
                (project["id"], kind, status, status, assignee, assignee),
            ).fetchall()
        finally:
            conn.close()
        return {
            "trusted_metadata": {
                "project_key": project_key,
                "kind": kind,
                "count": len(rows),
            },
            "claims": [self._serialize_work_claim(row, project_key) for row in rows],
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
                self._require_coordinator(project)
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
                row = self._work_claim_row(conn, project["id"], kind, claim_id)
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

    def import_work_claims(
        self,
        actor: dict[str, Any],
        project_key: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        project_key = self._valid_project_key(project_key)
        kind = self._valid_work_kind(kind)
        self._require_fields(payload, required={"external_ids"})
        values = payload["external_ids"]
        if not isinstance(values, list):
            raise ServiceError(
                400, "invalid_import", "external_ids must be a JSON array"
            )
        if len(values) > MAX_CLAIM_IMPORT_ENTRIES:
            raise ServiceError(
                400,
                "import_limit",
                f"an import may contain at most {MAX_CLAIM_IMPORT_ENTRIES} identifiers",
            )
        external_ids: list[str] = []
        seen: set[str] = set()
        for value in values:
            try:
                external_id = validate_external_id(value)
            except ValidationError as exc:
                raise self._validation_error(exc) from exc
            if external_id in seen:
                raise ServiceError(
                    400,
                    "duplicate_reference",
                    "an import must not contain duplicate identifiers",
                )
            seen.add(external_id)
            external_ids.append(external_id)

        key = self._valid_idempotency_key(idempotency_key)
        canonical = {"external_ids": external_ids}
        operation = f"work_claim.import:{project_key}:{kind}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                self._require_coordinator(project)
                rows = conn.execute(
                    """
                    SELECT external_id FROM work_claims
                    WHERE project_id = ? AND kind = ?
                    """,
                    (project["id"], kind),
                ).fetchall()
                registered = {row["external_id"] for row in rows}
                existing = set(external_ids) & registered
                missing = [value for value in external_ids if value not in existing]
                claim_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM work_claims WHERE project_id = ?",
                    (project["id"],),
                ).fetchone()["count"]
                if claim_count + len(missing) > MAX_WORK_CLAIMS_PER_PROJECT:
                    raise ServiceError(
                        429, "claim_limit", "project work-claim limit reached"
                    )

                now = self.db.now()
                created: list[dict[str, Any]] = []
                for external_id in missing:
                    claim_id = self.db.new_id("clm")
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
                            "source": "import",
                        },
                    )
                    row = self._work_claim_row(conn, project["id"], kind, claim_id)
                    created.append(self._serialize_work_claim(row, project_key))

                return 200, {
                    "trusted_metadata": {
                        "project_key": project_key,
                        "kind": kind,
                        "requested_count": len(external_ids),
                        "created_count": len(created),
                        "unchanged_count": len(existing),
                    },
                    "created": created,
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
                claim = self._work_claim_row(conn, project["id"], kind, claim_id)
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
                    current = self._work_claim_row(conn, project["id"], kind, claim_id)
                    raise ServiceError(
                        409,
                        "version_conflict",
                        "work-claim version has changed",
                        {
                            "expected_version": version,
                            "current_version": current["version"],
                        },
                    )
                self.db._audit(
                    conn,
                    actor["id"],
                    "work_claim.claimed",
                    claim_id,
                    {"project_id": project["id"], "kind": kind},
                )
                updated = self._work_claim_row(conn, project["id"], kind, claim_id)
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

    def assign_work(
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
        self._require_fields(payload, required={"assignee", "expected_version"})
        assignee_handle = self._valid_handle(payload["assignee"])
        version = self._valid_expected_version(payload["expected_version"])
        key = self._valid_idempotency_key(idempotency_key)
        canonical = {
            "assignee": assignee_handle,
            "expected_version": version,
        }
        operation = f"work_claim.assign:{project_key}:{kind}:{claim_id}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                self._require_coordinator(project)
                assignee = conn.execute(
                    """
                    SELECT u.id, u.handle
                    FROM project_members pm
                    JOIN users u ON u.id = pm.user_id
                    WHERE pm.project_id = ? AND u.handle = ? AND u.active = 1
                    """,
                    (project["id"], assignee_handle),
                ).fetchone()
                if assignee is None:
                    raise ServiceError(
                        404,
                        "assignee_not_found",
                        "active assignee is not a member of this project",
                    )
                claim = self._work_claim_row(conn, project["id"], kind, claim_id)
                self._require_claim_version(claim, version)
                if claim["status"] != "available":
                    raise ServiceError(
                        409, "already_claimed", "work is not available to assign"
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
                        assignee["id"],
                        now,
                        claim_id,
                        project["id"],
                        kind,
                        version,
                    ),
                )
                if cursor.rowcount != 1:
                    current = self._work_claim_row(conn, project["id"], kind, claim_id)
                    raise ServiceError(
                        409,
                        "version_conflict",
                        "work-claim version has changed",
                        {
                            "expected_version": version,
                            "current_version": current["version"],
                        },
                    )
                self.db._audit(
                    conn,
                    actor["id"],
                    "work_claim.assigned",
                    claim_id,
                    {
                        "project_id": project["id"],
                        "kind": kind,
                        "assignee_id": assignee["id"],
                    },
                )
                updated = self._work_claim_row(conn, project["id"], kind, claim_id)
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
                claim = self._work_claim_row(conn, project["id"], kind, claim_id)
                self._require_claim_version(claim, version)
                effective_status = claim["effective_status"]
                if effective_status not in {"claimed", "active", "under_review"}:
                    raise ServiceError(
                        409,
                        "invalid_transition",
                        "only claimed, active, or under-review work can be released",
                    )
                if effective_status == "under_review":
                    self._require_coordinator(project)
                elif (
                    claim["assignee_id"] != actor["id"]
                    and project["role"] not in COORDINATION_ROLES
                ):
                    raise ServiceError(
                        403,
                        "forbidden",
                        "only the assignee, a coordinator, or an admin may release work",
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
                    current = self._work_claim_row(conn, project["id"], kind, claim_id)
                    raise ServiceError(
                        409,
                        "version_conflict",
                        "work-claim version has changed",
                        {
                            "expected_version": version,
                            "current_version": current["version"],
                        },
                    )
                review_cleared = effective_status == "under_review"
                if review_cleared:
                    deleted = conn.execute(
                        "DELETE FROM work_claim_review_requests "
                        "WHERE project_id = ? AND claim_id = ?",
                        (project["id"], claim_id),
                    )
                    if deleted.rowcount != 1:
                        raise ServiceError(
                            409,
                            "version_conflict",
                            "work-claim review state has changed",
                            {
                                "expected_version": version,
                                "current_version": claim["version"],
                            },
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
                        "review_cleared": review_cleared,
                    },
                )
                updated = self._work_claim_row(conn, project["id"], kind, claim_id)
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
        if status not in WORK_STATUS_TARGETS:
            raise ServiceError(
                400,
                "invalid_status",
                "status must be active, under_review, or done",
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
                claim = self._work_claim_row(conn, project["id"], kind, claim_id)
                self._require_claim_version(claim, version)
                current_status = claim["effective_status"]
                now = self.db.now()
                audit_action: str

                if current_status == "claimed" and status == "active":
                    if claim["assignee_id"] != actor["id"]:
                        raise ServiceError(
                            403,
                            "forbidden",
                            "only the assignee may activate claimed work",
                        )
                    stored_status = "active"
                    audit_action = "work_claim.status_changed"
                elif current_status == "active" and status == "under_review":
                    if claim["assignee_id"] != actor["id"]:
                        raise ServiceError(
                            403,
                            "forbidden",
                            "only the assignee may submit work for review",
                        )
                    stored_status = "active"
                    audit_action = "work_claim.submitted"
                elif current_status == "under_review" and status in {"active", "done"}:
                    if claim["assignee_id"] == actor["id"]:
                        if status == "done":
                            raise ServiceError(
                                403,
                                "self_approval",
                                "an assignee cannot approve their own work",
                                {
                                    "required_roles": ["coordinator", "admin"],
                                    "assignee_may_act": False,
                                },
                            )
                        raise ServiceError(
                            403,
                            "forbidden",
                            "an assignee cannot review their own work",
                            {
                                "required_roles": ["coordinator", "admin"],
                                "assignee_may_act": False,
                            },
                        )
                    self._require_coordinator(project)
                    raise ServiceError(
                        409,
                        "review_verdict_required",
                        "review verdicts require the atomic claim-review operation",
                        {
                            "operation": "claim-review",
                            "allowed_actions": ["changes_requested", "approved"],
                        },
                    )
                else:
                    raise ServiceError(
                        409,
                        "invalid_transition",
                        "work-claim status transition is not permitted",
                    )

                cursor = conn.execute(
                    """
                    UPDATE work_claims
                    SET status = ?, version = version + 1,
                        review_round = review_round + ?, updated_at = ?
                    WHERE id = ? AND project_id = ? AND kind = ? AND version = ?
                      AND status = ?
                    """,
                    (
                        stored_status,
                        1 if status == "under_review" else 0,
                        now,
                        claim_id,
                        project["id"],
                        kind,
                        version,
                        claim["status"],
                    ),
                )
                if cursor.rowcount != 1:
                    current = self._work_claim_row(conn, project["id"], kind, claim_id)
                    raise ServiceError(
                        409,
                        "version_conflict",
                        "work-claim version has changed",
                        {
                            "expected_version": version,
                            "current_version": current["version"],
                        },
                    )

                if current_status == "active" and status == "under_review":
                    conn.execute(
                        """
                        INSERT INTO work_claim_review_requests(
                            project_id, claim_id, submitted_by, submitted_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (project["id"], claim_id, actor["id"], now),
                    )
                self.db._audit(
                    conn,
                    actor["id"],
                    audit_action,
                    claim_id,
                    {
                        "project_id": project["id"],
                        "kind": kind,
                        "from_status": current_status,
                        "to_status": status,
                        "review_round": (
                            claim["review_round"] + 1
                            if status == "under_review"
                            else claim["review_round"]
                        ),
                    },
                )
                updated = self._work_claim_row(conn, project["id"], kind, claim_id)
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

    def review_work(
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
        self._require_fields(
            payload,
            required={"action", "expected_version", "session_id", "body"},
        )
        action = payload["action"]
        if action not in {"changes_requested", "approved"}:
            raise ServiceError(
                400,
                "invalid_review_action",
                "action must be changes_requested or approved",
            )
        version = self._valid_expected_version(payload["expected_version"])
        session_id = self._valid_object_id(payload["session_id"], "ses")
        body = self._sanitized_text(
            payload["body"], field="body", max_bytes=MAX_BODY_BYTES
        )
        key = self._valid_idempotency_key(idempotency_key)
        canonical = {
            "action": action,
            "expected_version": version,
            "session_id": session_id,
            "body": body.text,
        }
        operation = f"work_claim.review:{project_key}:{kind}:{claim_id}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                claim = self._work_claim_row(conn, project["id"], kind, claim_id)
                self._require_claim_version(claim, version)
                if claim["effective_status"] != "under_review":
                    raise ServiceError(
                        409,
                        "invalid_transition",
                        "a verdict requires work currently under review",
                    )
                if claim["assignee_id"] == actor["id"]:
                    raise ServiceError(
                        403,
                        "self_approval",
                        "an assignee cannot review their own work",
                        {
                            "required_roles": ["coordinator", "admin"],
                            "assignee_may_act": False,
                        },
                    )
                self._require_coordinator(project)

                title_action = (
                    "Changes requested" if action == "changes_requested" else "Approved"
                )
                title = self._sanitized_text(
                    f"{title_action} (review round {claim['review_round']}): "
                    f"{claim['external_id']}",
                    field="title",
                    max_bytes=MAX_TITLE_BYTES,
                )
                flags = merge_risk_flags(title.risk_flags, body.risk_flags)
                notification = self._create_item_in_transaction(
                    conn,
                    project_id=project["id"],
                    session_id=session_id,
                    actor_id=actor["id"],
                    recipient_handle=claim["assignee_handle"],
                    claim_id=claim_id,
                    title=title.text,
                    body=body.text,
                    flags=flags,
                )

                target_status = "active" if action == "changes_requested" else "done"
                now = self.db.now()
                cursor = conn.execute(
                    """
                    UPDATE work_claims
                    SET status = ?, version = version + 1, updated_at = ?
                    WHERE id = ? AND project_id = ? AND kind = ? AND version = ?
                      AND status = 'active'
                    """,
                    (
                        target_status,
                        now,
                        claim_id,
                        project["id"],
                        kind,
                        version,
                    ),
                )
                if cursor.rowcount != 1:
                    current = self._work_claim_row(conn, project["id"], kind, claim_id)
                    raise ServiceError(
                        409,
                        "version_conflict",
                        "work-claim version has changed",
                        {
                            "expected_version": version,
                            "current_version": current["version"],
                        },
                    )
                deleted = conn.execute(
                    "DELETE FROM work_claim_review_requests "
                    "WHERE project_id = ? AND claim_id = ?",
                    (project["id"], claim_id),
                )
                if deleted.rowcount != 1:
                    raise ServiceError(
                        409,
                        "version_conflict",
                        "work-claim review state has changed",
                        {
                            "expected_version": version,
                            "current_version": version + 1,
                        },
                    )
                notification_meta = notification["trusted_metadata"]
                self.db._audit(
                    conn,
                    actor["id"],
                    f"work_claim.{action}",
                    claim_id,
                    {
                        "project_id": project["id"],
                        "kind": kind,
                        "from_status": "under_review",
                        "to_status": target_status,
                        "review_round": claim["review_round"],
                        "notification_item_id": notification_meta["item_id"],
                        "notification_event_seq": notification_meta["event_seq"],
                    },
                )
                updated = self._work_claim_row(conn, project["id"], kind, claim_id)
                return 200, {
                    "trusted_metadata": {
                        "action": action,
                        "review_round": claim["review_round"],
                        "notification_item_id": notification_meta["item_id"],
                        "notification_event_seq": notification_meta["event_seq"],
                    },
                    "claim": self._serialize_work_claim(updated, project_key),
                    "notification": notification,
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

    def get_work_claim(
        self,
        actor: dict[str, Any],
        project_key: str,
        kind: str,
        claim_id: str,
    ) -> dict[str, Any]:
        project_key = self._valid_project_key(project_key)
        kind = self._valid_work_kind(kind)
        claim_id = self._valid_object_id(claim_id, "clm")
        conn = self.db.connect()
        try:
            project = self._project_membership(conn, actor["id"], project_key)
            claim = self._work_claim_row(conn, project["id"], kind, claim_id)
            rows = conn.execute(
                """
                SELECT a.action, a.details_json, a.created_at,
                       u.id AS actor_id, u.handle AS actor_handle
                FROM audit_log a
                JOIN users u ON u.id = a.actor_user_id
                WHERE a.target = ? AND a.action IN (
                    'work_claim.submitted',
                    'work_claim.changes_requested',
                    'work_claim.approved'
                )
                ORDER BY a.seq
                """,
                (claim_id,),
            ).fetchall()
        finally:
            conn.close()

        history: list[dict[str, Any]] = []
        derived_round = 0
        for row in rows:
            details = json.loads(row["details_json"])
            if details.get("project_id") != project["id"]:
                continue
            stored_round = details.get("review_round")
            if row["action"] == "work_claim.submitted":
                if isinstance(stored_round, int) and stored_round > derived_round:
                    derived_round = stored_round
                else:
                    derived_round += 1
            review_round = (
                stored_round
                if isinstance(stored_round, int) and stored_round > 0
                else derived_round
            )
            entry = {
                "action": row["action"].removeprefix("work_claim."),
                "actor": {
                    "user_id": row["actor_id"],
                    "handle": row["actor_handle"],
                },
                "from_status": details.get("from_status"),
                "to_status": details.get("to_status"),
                "review_round": review_round,
                "created_at": row["created_at"],
            }
            if "notification_item_id" in details:
                entry["notification_item_id"] = details["notification_item_id"]
                entry["notification_event_seq"] = details["notification_event_seq"]
            history.append({"trusted_metadata": entry})
        return {
            "claim": self._serialize_work_claim(claim, project_key),
            "review_history": history,
        }

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
                response = self._create_item_in_transaction(
                    conn,
                    project_id=project["id"],
                    session_id=session_id,
                    actor_id=actor["id"],
                    recipient_handle=recipient_handle,
                    claim_id=claim_id,
                    title=title.text,
                    body=body.text,
                    flags=flags,
                )
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
                        409,
                        "version_conflict",
                        "work item version has changed",
                        {
                            "expected_version": version,
                            "current_version": item["version"],
                        },
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
                    current = self._item_row(
                        conn, project["id"], session_id, item_id, actor["id"]
                    )
                    raise ServiceError(
                        409,
                        "version_conflict",
                        "work item version has changed",
                        {
                            "expected_version": version,
                            "current_version": current["version"],
                        },
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

    def get_project_inbox(
        self,
        actor: dict[str, Any],
        project_key: str,
        *,
        after: int | None = 0,
        limit: int = 50,
        wait: int = 0,
        unread: bool = False,
    ) -> dict[str, Any]:
        project_key = self._valid_project_key(project_key)
        if not isinstance(unread, bool):
            raise ServiceError(400, "invalid_unread", "unread must be a boolean")
        if unread and after is not None:
            raise ServiceError(
                400,
                "invalid_cursor",
                "after and unread cannot be used together",
            )
        if after is None:
            after = 0
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
        rows: list[sqlite3.Row] = []
        effective_after = after
        acked_through = 0
        while True:
            conn = self.db.connect()
            try:
                project = self._project_membership(conn, actor["id"], project_key)
                cursor = conn.execute(
                    """
                    SELECT acked_through FROM project_inbox_cursors
                    WHERE project_id = ? AND user_id = ?
                    """,
                    (project["id"], actor["id"]),
                ).fetchone()
                acked_through = cursor["acked_through"] if cursor is not None else 0
                effective_after = acked_through if unread else after
                rows = conn.execute(
                    """
                    SELECT
                        e.*, i.title, i.body, i.status AS current_status, i.version,
                        i.work_claim_id,
                        wc.kind AS work_claim_kind,
                        wc.external_id AS work_claim_external_id,
                        creator.id AS creator_id, creator.handle AS creator_handle,
                        recipient.id AS recipient_id,
                        recipient.handle AS recipient_handle,
                        event_actor.handle AS actor_handle
                    FROM deliveries d
                    JOIN events e ON e.seq = d.event_seq
                    JOIN items i ON i.id = e.item_id
                    JOIN users creator ON creator.id = i.creator_id
                    JOIN users recipient ON recipient.id = i.recipient_id
                    JOIN users event_actor ON event_actor.id = e.actor_id
                    JOIN session_members reader_scope
                      ON reader_scope.project_id = e.project_id
                     AND reader_scope.session_id = e.session_id
                     AND reader_scope.user_id = d.user_id
                    LEFT JOIN work_claims wc ON wc.id = i.work_claim_id
                    WHERE d.user_id = ? AND e.project_id = ? AND e.seq > ?
                    ORDER BY e.seq
                    LIMIT ?
                    """,
                    (
                        actor["id"],
                        project["id"],
                        effective_after,
                        limit,
                    ),
                ).fetchall()
            finally:
                conn.close()
            if rows or time.monotonic() >= deadline:
                break
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

        return {
            "trusted_metadata": {
                "project_key": project_key,
                "after": effective_after,
                "next_cursor": rows[-1]["seq"] if rows else effective_after,
                "acked_through": acked_through,
                "unread": unread,
            },
            "events": [self._serialize_queue_event(row, project_key) for row in rows],
        }

    def ack_project_inbox(
        self,
        actor: dict[str, Any],
        project_key: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> MutationResult:
        project_key = self._valid_project_key(project_key)
        self._require_fields(payload, required={"through"})
        through = payload["through"]
        if isinstance(through, bool) or not isinstance(through, int) or through < 0:
            raise ServiceError(
                400, "invalid_cursor", "through must be a non-negative integer"
            )
        key = self._valid_idempotency_key(idempotency_key)
        canonical = {"through": through}
        operation = f"project_inbox.ack:{project_key}"
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def mutate() -> tuple[int, dict[str, Any]]:
                project = self._project_membership(conn, actor["id"], project_key)
                maximum = conn.execute(
                    """
                    SELECT COALESCE(MAX(e.seq), 0) AS max_seq
                    FROM deliveries d
                    JOIN events e ON e.seq = d.event_seq
                    WHERE d.user_id = ? AND e.project_id = ?
                    """,
                    (actor["id"], project["id"]),
                ).fetchone()["max_seq"]
                if through > maximum:
                    raise ServiceError(
                        409,
                        "ack_beyond_delivery",
                        "cannot acknowledge beyond the latest delivered event",
                        {"requested_ack": through, "max_delivered_seq": maximum},
                    )
                current_row = conn.execute(
                    """
                    SELECT acked_through FROM project_inbox_cursors
                    WHERE project_id = ? AND user_id = ?
                    """,
                    (project["id"], actor["id"]),
                ).fetchone()
                current = current_row["acked_through"] if current_row is not None else 0
                if through < current:
                    raise ServiceError(
                        409,
                        "ack_regression",
                        "inbox acknowledgement cannot move backwards",
                        {"requested_ack": through, "current_ack": current},
                    )
                now = self.db.now()
                conn.execute(
                    """
                    INSERT INTO project_inbox_cursors(
                        project_id, user_id, acked_through, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(project_id, user_id) DO UPDATE SET
                        acked_through = excluded.acked_through,
                        updated_at = excluded.updated_at
                    """,
                    (project["id"], actor["id"], through, now),
                )
                self.db._audit(
                    conn,
                    actor["id"],
                    "project_inbox.acknowledged",
                    project["id"],
                    {"from_seq": current, "through": through},
                )
                return 200, {
                    "trusted_metadata": {
                        "project_key": project_key,
                        "acked_through": through,
                        "updated_at": now,
                    }
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

    def _project_membership(
        self, conn: sqlite3.Connection, user_id: str, project_key: str
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT
                p.id,
                p.project_key,
                CASE
                    WHEN pm.role = 'admin' THEN 'admin'
                    WHEN pc.user_id IS NOT NULL THEN 'coordinator'
                    ELSE 'member'
                END AS role
            FROM projects p
            JOIN project_members pm ON pm.project_id = p.id
            JOIN users u ON u.id = pm.user_id
            LEFT JOIN project_coordinators pc
              ON pc.project_id = pm.project_id AND pc.user_id = pm.user_id
            WHERE p.project_key = ? AND pm.user_id = ? AND u.active = 1
            """,
            (project_key, user_id),
        ).fetchone()
        if row is None:
            raise ServiceError(404, "scope_not_found", "project scope not found")
        return row

    @staticmethod
    def _require_coordinator(project: sqlite3.Row) -> None:
        if project["role"] not in COORDINATION_ROLES:
            raise ServiceError(
                403,
                "forbidden",
                "project coordinator or admin role required",
                {"required_roles": ["coordinator", "admin"]},
            )

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
                CASE
                    WHEN review.claim_id IS NOT NULL THEN 'under_review'
                    ELSE wc.status
                END AS effective_status,
                review.submitted_by AS review_submitted_by,
                review.submitted_at AS review_submitted_at,
                creator.handle AS creator_handle,
                assignee.handle AS assignee_handle
            FROM work_claims wc
            LEFT JOIN work_claim_review_requests review
              ON review.project_id = wc.project_id AND review.claim_id = wc.id
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
                409,
                "version_conflict",
                "work-claim version has changed",
                {
                    "expected_version": expected_version,
                    "current_version": claim["version"],
                },
            )

    def _create_item_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        session_id: str,
        actor_id: str,
        recipient_handle: str,
        claim_id: str | None,
        title: str,
        body: str,
        flags: tuple[str, ...],
    ) -> dict[str, Any]:
        self._session_membership(conn, actor_id, project_id, session_id)
        if claim_id is not None:
            linked_claim = conn.execute(
                "SELECT 1 FROM work_claims WHERE id = ? AND project_id = ?",
                (claim_id, project_id),
            ).fetchone()
            if linked_claim is None:
                raise ServiceError(
                    404,
                    "work_claim_not_found",
                    "work claim not found in this project",
                )
        session = conn.execute(
            "SELECT state FROM sessions WHERE id = ? AND project_id = ?",
            (session_id, project_id),
        ).fetchone()
        if session is None:
            raise ServiceError(404, "scope_not_found", "session scope not found")
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
            (recipient_handle, project_id, session_id),
        ).fetchone()
        if recipient is None:
            raise ServiceError(
                404,
                "recipient_not_found",
                "recipient is not an active session participant",
            )
        if recipient["id"] == actor_id:
            raise ServiceError(400, "self_assignment", "recipient must be another user")
        open_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM items
            WHERE project_id = ? AND session_id = ?
              AND creator_id = ? AND recipient_id = ? AND status LIKE 'open.%'
            """,
            (project_id, session_id, actor_id, recipient["id"]),
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
                project_id,
                session_id,
                actor_id,
                recipient["id"],
                claim_id,
                title,
                body,
                self._json_list(flags),
                now,
                now,
            ),
        )
        event_seq = self._insert_event(
            conn,
            project_id,
            session_id,
            item_id,
            actor_id,
            "item.created",
            None,
            "open.pending",
            None,
            flags,
            recipient["id"],
        )
        row = self._item_row(conn, project_id, session_id, item_id, actor_id)
        response = self._serialize_item(row)
        response["trusted_metadata"]["event_seq"] = event_seq
        return response

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
    def _serialize_work_claim(row: sqlite3.Row, project_key: str) -> dict[str, Any]:
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
                "status": row["effective_status"],
                "version": row["version"],
                "review_round": row["review_round"],
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

    def _valid_handle(self, value: object) -> str:
        try:
            return validate_handle(value)
        except ValidationError as exc:
            raise self._validation_error(exc) from exc

    @staticmethod
    def _valid_work_kind(value: object) -> str:
        if not isinstance(value, str) or value not in WORK_KINDS:
            raise ServiceError(400, "invalid_kind", "unknown work-claim kind")
        return value

    @staticmethod
    def _valid_work_status(value: object) -> str:
        if not isinstance(value, str) or value not in WORK_STATUSES:
            raise ServiceError(400, "invalid_status", "unknown work-claim status")
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
