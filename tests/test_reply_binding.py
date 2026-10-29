from __future__ import annotations

import json
import sqlite3

from test_service import ServiceFixture

from imitation_krab.db import SCHEMA_VERSION
from imitation_krab.service import ServiceError


_OMIT = object()


class ReplyBindingTests(ServiceFixture):
    def setUp(self) -> None:
        super().setUp()
        _, carol_token = self.db.create_user("carol")
        self.db.add_project_member("project-one", "carol", "member")
        self.carol = self.db.authenticate(carol_token)
        assert self.carol
        self.wide_session_id = self.service.create_session(
            self.alice,
            "project-one",
            {
                "label": "Three participants",
                "participants": ["alice", "bob", "carol"],
            },
            "reply-wide-session-0001",
        ).payload["trusted_metadata"]["session_id"]

    def send(
        self,
        actor: dict,
        to: str,
        key: str,
        *,
        reply_to: object = _OMIT,
        project: str = "project-one",
        session: str | None = None,
        title: str = "Message",
        body: str = "Body",
    ):
        payload: dict[str, object] = {"to": to, "title": title, "body": body}
        if reply_to is not _OMIT:
            payload["in_reply_to_item_id"] = reply_to
        return self.service.create_item(
            actor,
            project,
            session or self.session_id,
            payload,
            key,
        )

    def counts(self) -> tuple[int, int, int]:
        conn = self.db.connect()
        try:
            return tuple(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("items", "events", "idempotency_keys")
            )
        finally:
            conn.close()

    def test_reply_is_attested_on_private_surfaces_but_not_project_activity(self):
        request = self.send(
            self.alice,
            "bob",
            "reply-request-0001",
            title="Approval needed",
            body="Please decide.",
        ).payload
        request_id = request["trusted_metadata"]["item_id"]
        self.assertIsNone(request["trusted_metadata"]["in_reply_to_item_id"])

        reply = self.send(
            self.bob,
            "alice",
            "reply-response-0001",
            reply_to=request_id,
            title="Approval response",
            body="Approved.",
        ).payload
        reply_id = reply["trusted_metadata"]["item_id"]
        self.assertEqual(
            reply["trusted_metadata"]["in_reply_to_item_id"], request_id
        )

        for actor in (self.alice, self.bob):
            shown = self.service.get_item(
                actor, "project-one", self.session_id, reply_id
            )
            self.assertEqual(
                shown["trusted_metadata"]["in_reply_to_item_id"], request_id
            )

        for result in (
            self.service.get_queue(self.alice, "project-one", self.session_id),
            self.service.get_project_inbox(self.alice, "project-one"),
        ):
            event = next(
                current
                for current in result["events"]
                if current["trusted_metadata"]["item_id"] == reply_id
            )
            self.assertEqual(
                event["trusted_metadata"]["in_reply_to_item_id"], request_id
            )

        activity = self.service.get_project_activity(self.alice, "project-one")
        event = next(
            current
            for current in activity["events"]
            if current["trusted_metadata"]["item_id"] == reply_id
        )
        self.assertNotIn("in_reply_to_item_id", event["trusted_metadata"])

    def test_invalid_inaccessible_and_cross_scope_targets_fail_atomically(self):
        request = self.send(
            self.alice,
            "bob",
            "reply-validation-request-0001",
            session=self.wide_session_id,
        ).payload["trusted_metadata"]["item_id"]
        private = self.send(
            self.alice,
            "carol",
            "reply-private-request-0001",
            session=self.wide_session_id,
        ).payload["trusted_metadata"]["item_id"]
        other_session = self.send(
            self.alice,
            "bob",
            "reply-other-session-0001",
        ).payload["trusted_metadata"]["item_id"]

        self.db.add_project_member("project-two", "alice", "member")
        self.db.add_project_member("project-two", "bob", "member")
        project_two_session = self.service.create_session(
            self.alice,
            "project-two",
            {"label": "Other project", "participants": ["alice", "bob"]},
            "reply-project-two-session-0001",
        ).payload["trusted_metadata"]["session_id"]
        other_project = self.send(
            self.alice,
            "bob",
            "reply-other-project-0001",
            project="project-two",
            session=project_two_session,
        ).payload["trusted_metadata"]["item_id"]

        attempts = (
            (self.bob, "alice", None, "reply-null-0001", 400, "validation_error"),
            (self.bob, "alice", "bad", "reply-malformed-0001", 400, "validation_error"),
            (
                self.bob,
                "alice",
                "itm_" + "0" * 32,
                "reply-missing-0001",
                404,
                "item_not_found",
            ),
            (
                self.alice,
                "bob",
                request,
                "reply-wrong-actor-0001",
                404,
                "item_not_found",
            ),
            (
                self.bob,
                "carol",
                request,
                "reply-wrong-recipient-0001",
                404,
                "item_not_found",
            ),
            (
                self.bob,
                "alice",
                private,
                "reply-inaccessible-0001",
                404,
                "item_not_found",
            ),
            (
                self.bob,
                "alice",
                other_session,
                "reply-cross-session-0001",
                404,
                "item_not_found",
            ),
            (
                self.bob,
                "alice",
                other_project,
                "reply-cross-project-0001",
                404,
                "item_not_found",
            ),
        )
        for actor, recipient, target, key, status, code in attempts:
            with self.subTest(key=key):
                before = self.counts()
                with self.assertRaises(ServiceError) as caught:
                    self.send(
                        actor,
                        recipient,
                        key,
                        reply_to=target,
                        session=self.wide_session_id,
                    )
                self.assertEqual((caught.exception.status, caught.exception.code),
                                 (status, code))
                self.assertEqual(self.counts(), before)

    def test_idempotency_multiple_replies_chains_and_terminal_targets(self):
        first_request = self.send(
            self.alice, "bob", "reply-first-request-0001"
        ).payload["trusted_metadata"]["item_id"]
        second_request = self.send(
            self.alice, "bob", "reply-second-request-0001"
        ).payload["trusted_metadata"]["item_id"]

        first = self.send(
            self.bob,
            "alice",
            "reply-stable-response-0001",
            reply_to=first_request,
        )
        replay = self.send(
            self.bob,
            "alice",
            "reply-stable-response-0001",
            reply_to=first_request,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.payload, first.payload)
        before = self.counts()
        with self.assertRaises(ServiceError) as caught:
            self.send(
                self.bob,
                "alice",
                "reply-stable-response-0001",
                reply_to=second_request,
            )
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual(self.counts(), before)

        second_reply = self.send(
            self.bob,
            "alice",
            "reply-second-response-0001",
            reply_to=first_request,
        ).payload
        self.assertNotEqual(
            first.payload["trusted_metadata"]["item_id"],
            second_reply["trusted_metadata"]["item_id"],
        )
        chained = self.send(
            self.alice,
            "bob",
            "reply-chain-0001",
            reply_to=first.payload["trusted_metadata"]["item_id"],
        ).payload
        self.assertEqual(
            chained["trusted_metadata"]["in_reply_to_item_id"],
            first.payload["trusted_metadata"]["item_id"],
        )

        transitions = (
            (self.bob, "open.in_progress", 1),
            (self.bob, "open.under_review", 2),
            (self.alice, "open.approved", 3),
            (self.alice, "closed.approved", 4),
        )
        for number, (actor, status, version) in enumerate(transitions, start=1):
            self.service.change_status(
                actor,
                "project-one",
                self.session_id,
                first_request,
                {"status": status, "expected_version": version},
                f"reply-terminal-transition-{number:04d}",
            )
        terminal_reply = self.send(
            self.bob,
            "alice",
            "reply-terminal-target-0001",
            reply_to=first_request,
        ).payload
        self.assertEqual(
            terminal_reply["trusted_metadata"]["in_reply_to_item_id"], first_request
        )

    def test_storage_enforces_route_and_immutability(self):
        request = self.send(
            self.alice, "bob", "reply-storage-request-0001"
        ).payload["trusted_metadata"]["item_id"]
        reply = self.send(
            self.bob,
            "alice",
            "reply-storage-response-0001",
            reply_to=request,
        ).payload["trusted_metadata"]["item_id"]

        conn = self.db.connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE items SET in_reply_to_item_id = NULL WHERE id = ?",
                    (reply,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO items(
                        id, project_id, session_id, creator_id, recipient_id,
                        work_claim_id, title, body, risk_flags, completion_policy,
                        status, version, created_at, updated_at,
                        in_reply_to_item_id
                    )
                    SELECT ?, project_id, session_id, creator_id, recipient_id,
                           work_claim_id, title, body, risk_flags, completion_policy,
                           status, version, created_at, updated_at, ?
                    FROM items WHERE id = ?
                    """,
                    ("itm_" + "f" * 32, request, request),
                )
        finally:
            conn.close()

    def test_legacy_idempotent_replay_gets_null_without_rewriting_storage(self):
        created = self.send(
            self.alice, "bob", "reply-legacy-replay-0001"
        ).payload
        conn = self.db.connect()
        try:
            stored = conn.execute(
                "SELECT response_json FROM idempotency_keys WHERE key = ?",
                ("reply-legacy-replay-0001",),
            ).fetchone()[0]
            legacy = json.loads(stored)
            del legacy["trusted_metadata"]["in_reply_to_item_id"]
            conn.execute(
                "UPDATE idempotency_keys SET response_json = ? WHERE key = ?",
                (json.dumps(legacy), "reply-legacy-replay-0001"),
            )
        finally:
            conn.close()
        before = self.counts()
        replay = self.send(
            self.alice, "bob", "reply-legacy-replay-0001"
        )
        self.assertTrue(replay.replayed)
        self.assertIsNone(replay.payload["trusted_metadata"]["in_reply_to_item_id"])
        self.assertEqual(self.counts(), before)
        self.assertEqual(
            created["trusted_metadata"]["item_id"],
            replay.payload["trusted_metadata"]["item_id"],
        )

    def test_schema_seven_migrates_existing_items_to_null_reply_links(self):
        existing = self.send(
            self.alice, "bob", "reply-migration-item-0001"
        ).payload["trusted_metadata"]["item_id"]
        conn = self.db.connect()
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            original = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'"
            ).fetchone()[0]
            legacy = original.replace("CREATE TABLE items", "CREATE TABLE items_v7", 1)
            legacy = legacy.replace("    in_reply_to_item_id TEXT,\n", "")
            legacy = legacy.replace(
                "    FOREIGN KEY (work_claim_id) REFERENCES work_claims(id) ON DELETE RESTRICT,\n"
                "    FOREIGN KEY (in_reply_to_item_id) REFERENCES items(id) ON DELETE RESTRICT\n",
                "    FOREIGN KEY (work_claim_id) REFERENCES work_claims(id) ON DELETE RESTRICT\n",
            )
            reply_triggers = {
                "items_reply_route_insert",
                "items_reply_link_immutable",
            }
            dependents = [
                row["sql"]
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE tbl_name = 'items' AND type IN ('index', 'trigger') "
                    "AND sql IS NOT NULL"
                )
                if row["name"] not in reply_triggers
            ]
            columns = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(items)")
                if row["name"] != "in_reply_to_item_id"
            ]
            names = ", ".join(f'"{name}"' for name in columns)
            conn.execute(legacy)
            conn.execute(f"INSERT INTO items_v7 ({names}) SELECT {names} FROM items")
            conn.execute("DROP TABLE items")
            conn.execute("ALTER TABLE items_v7 RENAME TO items")
            for statement in dependents:
                conn.execute(statement)
            conn.execute("UPDATE schema_meta SET version = 7")
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.close()

        self.db.initialize()
        self.db.initialize()
        self.assertEqual(SCHEMA_VERSION, 8)
        conn = self.db.connect()
        try:
            self.assertEqual(
                conn.execute("SELECT version FROM schema_meta").fetchone()[0], 8
            )
            row = conn.execute(
                "SELECT in_reply_to_item_id FROM items WHERE id = ?", (existing,)
            ).fetchone()
            self.assertIsNone(row["in_reply_to_item_id"])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            foreign_keys = conn.execute("PRAGMA foreign_key_list(items)").fetchall()
            self.assertTrue(
                any(
                    current["from"] == "in_reply_to_item_id"
                    and current["table"] == "items"
                    for current in foreign_keys
                )
            )
            trigger_names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND tbl_name = 'items'"
                )
            }
            self.assertTrue(
                {"items_reply_route_insert", "items_reply_link_immutable"}
                <= trigger_names
            )
        finally:
            conn.close()
