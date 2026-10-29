from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

from test_service import ServiceFixture
from imitation_krab.db import DatabaseError, SCHEMA_VERSION
from imitation_krab.service import ServiceError


class ReceiptTests(ServiceFixture):
    def receipt(self, policy="receipt", key="receipt-create-0001", claim_id=None):
        payload = {"to": "bob", "title": "Verdict", "body": "Reviewed"}
        if policy is not None:
            payload["completion_policy"] = policy
        if claim_id is not None:
            payload["claim_id"] = claim_id
        return self.service.create_item(
            self.alice, "project-one", self.session_id, payload, key
        ).payload["trusted_metadata"]

    def change(self, item, actor=None, status="closed.handled", version=1,
               key="receipt-handle-0001"):
        return self.service.change_status(
            actor or self.bob, "project-one", self.session_id, item,
            {"status": status, "expected_version": version, "note": "Test"}, key
        )

    def snapshot(self):
        with self.db.connect(initialize=False) as conn:
            return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                    for table in ("items", "events", "deliveries", "idempotency_keys",
                                  "work_claims", "work_claim_review_requests")}

    def test_handle_replays_and_notifies_creator_without_changing_claim(self):
        claim = self.create_claim().payload["trusted_metadata"]["claim_id"]
        meta = self.receipt(claim_id=claim)
        before = self.snapshot()
        result = self.change(meta["item_id"])
        self.assertEqual(result.payload["trusted_metadata"]["status"], "closed.handled")
        self.assertEqual(result.payload["trusted_metadata"]["completion_policy"], "receipt")
        self.assertEqual(result.payload, self.change(meta["item_id"]).payload)
        after = self.snapshot()
        self.assertEqual(before["work_claims"], after["work_claims"])
        self.assertEqual(before["work_claim_review_requests"], after["work_claim_review_requests"])
        self.assertEqual(len(after["events"]), len(before["events"]) + 1)
        self.assertEqual(after["deliveries"][-1][1], self.alice["id"])
        for actor, version in ((self.bob, 1), (self.bob, 2), (self.alice, 2)):
            with self.assertRaises(ServiceError):
                self.change(meta["item_id"], actor, version=version, key="another-handle-0001")
            self.assertEqual(after, self.snapshot())

    def test_defaults_and_policy_validation(self):
        self.assertEqual(self.receipt(None)["completion_policy"], "workflow")
        for policy in ("invalid", 1, [], {}):
            with self.assertRaises(ServiceError):
                self.receipt(policy, key="invalid-policy-0001")
        with self.assertRaises(ServiceError):
            self.service.create_item(self.alice, "project-one", self.session_id,
                {"to": "bob", "title": "T", "body": "B", "completion_policy": None},
                "invalid-policy-0001")

    def test_legacy_idempotent_replay_exposes_workflow_without_rewriting_history(self):
        self.receipt(None)
        with self.db.connect() as conn:
            stored = conn.execute("SELECT response_json FROM idempotency_keys WHERE key = 'receipt-create-0001'").fetchone()[0]
            legacy = json.loads(stored)
            del legacy["trusted_metadata"]["completion_policy"]
            conn.execute("UPDATE idempotency_keys SET response_json = ? WHERE key = 'receipt-create-0001'", (json.dumps(legacy),))
        before = self.snapshot()
        self.assertEqual(self.receipt("workflow")["completion_policy"], "workflow")
        self.assertEqual(before, self.snapshot())

    def test_cross_lifecycle_and_wrong_actor_fail_atomically(self):
        receipt = self.receipt()["item_id"]
        workflow = self.receipt("workflow", key="workflow-create-0001")["item_id"]
        for item, actor, status, version in (
            (receipt, self.mallory, "closed.handled", 1),
            (receipt, self.alice, "closed.handled", 1),
            (receipt, self.bob, "closed.handled", 2),
            (workflow, self.bob, "closed.handled", 1),
            *[(receipt, actor, status, 1)
              for actor in (self.alice, self.bob)
              for status in ("open.in_progress", "open.under_review", "open.pending",
                             "open.needs_changes", "open.approved", "closed.approved",
                             "closed.rejected")],
        ):
            with self.subTest(status=status, actor=actor["handle"]):
                before = self.snapshot()
                with self.assertRaises(ServiceError):
                    self.change(item, actor, status, version, key="invalid-change-0001")
                self.assertEqual(before, self.snapshot())

    def test_policy_is_immutable_in_storage(self):
        item = self.receipt()["item_id"]
        with self.db.connect() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE items SET completion_policy = 'workflow' WHERE id = ?", (item,))

    def test_policy_on_all_surfaces_and_ack_does_not_handle(self):
        item = self.receipt()["item_id"]
        before = self.snapshot()
        shown = self.service.get_item(self.bob, "project-one", self.session_id, item)
        self.assertEqual(shown["trusted_metadata"]["completion_policy"], "receipt")
        for result in (
            self.service.get_queue(self.bob, "project-one", self.session_id),
            self.service.get_project_inbox(self.bob, "project-one"),
            self.service.get_project_activity(self.bob, "project-one"),
        ):
            self.assertEqual(result["events"][0]["trusted_metadata"]["completion_policy"], "receipt")
        self.assertEqual(before, self.snapshot())
        self.service.ack_project_inbox(self.bob, "project-one",
            {"through": shown["history"][0]["trusted_metadata"]["event_seq"]},
            "receipt-ack-0001")
        self.assertEqual(before["items"], self.snapshot()["items"])

    def test_route_limit_has_only_bounded_diagnostics_and_handling_frees_space(self):
        for number in range(100):
            meta = self.receipt(key=f"receipt-limit-{number:04d}")
        before = self.snapshot()
        with self.assertRaises(ServiceError) as caught:
            self.receipt(key="receipt-limit-overflow")
        self.assertEqual(caught.exception.code, "queue_limit")
        self.assertEqual(caught.exception.details, {"limit": 100, "open_items": 100})
        self.assertEqual(before, self.snapshot())
        self.change(meta["item_id"])
        self.receipt(key="receipt-limit-overflow")

    def legacy_v6(self):
        claim = self.create_claim().payload["trusted_metadata"]["claim_id"]
        item = self.receipt("workflow", claim_id=claim)["item_id"]
        self.change(item, status="open.in_progress")
        conn = self.db.connect()
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            original = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'items'").fetchone()[0]
            legacy = original.replace("CREATE TABLE items", "CREATE TABLE items_v6", 1)
            legacy = legacy.replace("    completion_policy TEXT NOT NULL DEFAULT 'workflow'\n        CHECK (completion_policy IN ('workflow', 'receipt')),\n", "")
            legacy = legacy.replace(
                "    CHECK (\n        (completion_policy = 'workflow' AND status <> 'closed.handled')\n"
                "        OR (completion_policy = 'receipt' AND status IN ('open.pending', 'closed.handled'))\n    ),\n", "")
            legacy = legacy.replace(",\n        'closed.handled'", "")
            dependents = [row[0] for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name = 'items' AND sql IS NOT NULL "
                "AND type IN ('index', 'trigger') AND name <> 'items_completion_policy_immutable'")]
            names = ", ".join(row["name"] for row in conn.execute("PRAGMA table_info(items)")
                              if row["name"] != "completion_policy")
            conn.execute(legacy)
            conn.execute(f"INSERT INTO items_v6 ({names}) SELECT {names} FROM items")
            conn.execute("DROP TABLE items")
            conn.execute("ALTER TABLE items_v6 RENAME TO items")
            for statement in dependents:
                conn.execute(statement)
            conn.execute("UPDATE schema_meta SET version = 6")
            conn.commit()
        finally:
            conn.close()
        return item

    def test_v6_rebuild_preserves_history_deliveries_versions_claim_links_and_indexes(self):
        item = self.legacy_v6()
        before = self.snapshot()
        self.db.initialize()

        self.db.initialize()
        after = self.snapshot()
        for table in before.keys() - {"items"}:
            self.assertEqual(before[table], after[table])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                conn.execute("SELECT version FROM schema_meta").fetchone()[0],
                SCHEMA_VERSION,
            )
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone()
            self.assertEqual(row["completion_policy"], "workflow")
            self.assertEqual(tuple(row[column["name"]] for column in conn.execute(
                "PRAGMA table_info(items)") if column["name"] != "completion_policy"), before["items"][0])
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_items_recipient_status'").fetchone())
            for statement in (
                "UPDATE items SET completion_policy = 'receipt'",
                "UPDATE items SET status = 'closed.handled'",
                "UPDATE items SET work_claim_id = NULL",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(statement)
        self.receipt(key="post-migration-receipt")

    def test_foreign_key_failure_rolls_back_and_restores_enforcement(self):
        self.legacy_v6()
        with self.db.connect(initialize=False) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("UPDATE items SET recipient_id = 'usr_missing'")
            conn.execute("PRAGMA foreign_keys = ON")
            before = self.snapshot()
            with self.assertRaisesRegex(DatabaseError, "foreign-key integrity"):
                self.db._migrate_schema(conn)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT version FROM schema_meta").fetchone()[0], 6)
            self.assertNotIn("completion_policy", [row["name"] for row in conn.execute("PRAGMA table_info(items)")])
            self.assertEqual(before, self.snapshot())

    def test_handling_without_note_and_policy_idempotency_conflict(self):
        meta = self.receipt()
        before = self.snapshot()
        with self.assertRaises(ServiceError) as caught:
            self.receipt("workflow")
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual(before, self.snapshot())
        result = self.service.change_status(self.bob, "project-one", self.session_id,
            meta["item_id"], {"status": "closed.handled", "expected_version": 1},
            "handle-without-note")
        self.assertEqual(result.payload["trusted_metadata"]["status"], "closed.handled")

    def test_migration_failure_rolls_back_all_schema_and_data_changes(self):
        self.legacy_v6()
        before = self.snapshot()
        original = self.db._migrate_receipt_policy

        def fail_after_rebuild(conn):
            original(conn)
            raise RuntimeError("injected migration failure")

        with patch.object(self.db, "_migrate_receipt_policy", side_effect=fail_after_rebuild):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.db.initialize()
        self.assertEqual(before, self.snapshot())
        with self.db.connect(initialize=False) as conn:
            self.assertEqual(conn.execute("SELECT version FROM schema_meta").fetchone()[0], 6)
            self.assertNotIn("completion_policy", [row["name"] for row in conn.execute("PRAGMA table_info(items)")])
        self.db.initialize()
