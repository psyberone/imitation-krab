from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
except ImportError:  # The runtime deliberately has no third-party dependencies.
    Draft202012Validator = None  # type: ignore[assignment]
    Registry = None  # type: ignore[assignment]
    Resource = None  # type: ignore[assignment]

from imitation_krab.db import Database
from imitation_krab.service import Service


class SchemaFileTests(unittest.TestCase):
    def test_schema_catalog_is_parseable_and_all_local_refs_exist(self) -> None:
        root = Path(__file__).resolve().parents[1] / "schemas" / "v1"
        expected = {
            "claim-detail.schema.json",
            "claim-import.schema.json",
            "claim-list.schema.json",
            "claim.schema.json",
            "error.schema.json",
            "inbox-ack.schema.json",
            "item.schema.json",
            "project-list.schema.json",
            "queue.schema.json",
            "resources.schema.json",
            "review-result.schema.json",
            "session-list.schema.json",
            "session.schema.json",
        }
        self.assertEqual({path.name for path in root.glob("*.json")}, expected)

        def refs(value: object):
            if isinstance(value, dict):
                if isinstance(value.get("$ref"), str):
                    yield value["$ref"]
                for nested in value.values():
                    yield from refs(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from refs(nested)

        for path in root.glob("*.json"):
            with self.subTest(schema=path.name):
                document = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    document["$schema"],
                    "https://json-schema.org/draft/2020-12/schema",
                )
                self.assertTrue(document["$id"].endswith("/" + path.name))
                for reference in refs(document):
                    file_part = reference.split("#", 1)[0]
                    if file_part and "://" not in file_part:
                        self.assertTrue((root / file_part).is_file(), reference)


@unittest.skipUnless(
    Draft202012Validator is not None,
    "install the optional jsonschema package to validate response examples",
)
class ResponseSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.db = Database(root / "krab.db", root / "pepper.key")
        self.db.initialize()
        _, alice_token = self.db.create_user("alice")
        _, bob_token = self.db.create_user("bob")
        self.db.create_project("project-one", "Project One")
        self.db.add_project_member("project-one", "alice", "admin")
        self.db.add_project_member("project-one", "bob", "member")
        self.alice = self.db.authenticate(alice_token)
        self.bob = self.db.authenticate(bob_token)
        assert self.alice and self.bob
        self.service = Service(self.db)

        schema_root = Path(__file__).resolve().parents[1] / "schemas" / "v1"
        self.schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in schema_root.glob("*.json")
        }
        resources = [
            (document["$id"], Resource.from_contents(document))
            for document in self.schemas.values()
        ]
        self.registry = Registry().with_resources(resources)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def validate(self, schema_name: str, instance: dict) -> None:
        schema = self.schemas[schema_name]
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, registry=self.registry).validate(instance)

    def test_live_service_responses_match_the_catalog(self) -> None:
        self.validate(
            "project-list.schema.json", self.service.list_projects(self.alice)
        )

        session = self.service.create_session(
            self.alice,
            "project-one",
            {"label": "Review", "participants": ["bob"]},
            "schema-session-0001",
        ).payload
        session_id = session["trusted_metadata"]["session_id"]
        self.validate("session.schema.json", session)
        self.validate(
            "session-list.schema.json",
            self.service.list_sessions(self.bob, "project-one"),
        )

        claim = self.service.create_work_claim(
            self.alice,
            "project-one",
            "issue",
            {"external_id": "owner/repository#700"},
            "schema-claim-0001",
        ).payload
        claim_id = claim["trusted_metadata"]["claim_id"]
        self.validate("claim.schema.json", claim)
        imported = self.service.import_work_claims(
            self.alice,
            "project-one",
            "issue",
            {"external_ids": ["owner/repository#701", "owner/repository#702"]},
            "schema-claim-import-0001",
        ).payload
        self.validate("claim-import.schema.json", imported)
        self.validate(
            "claim-list.schema.json",
            self.service.list_work_claims(self.bob, "project-one", "issue"),
        )

        item = self.service.create_item(
            self.alice,
            "project-one",
            session_id,
            {
                "to": "bob",
                "title": "Review",
                "body": "Please review.",
                "claim_id": claim_id,
            },
            "schema-item-0001",
        ).payload
        self.validate("item.schema.json", item)
        self.validate(
            "item.schema.json",
            self.service.get_item(
                self.bob, "project-one", session_id, item["trusted_metadata"]["item_id"]
            ),
        )
        self.validate(
            "queue.schema.json",
            self.service.get_queue(self.bob, "project-one", session_id),
        )
        self.validate(
            "queue.schema.json",
            self.service.get_project_inbox(self.bob, "project-one"),
        )

        self.service.claim_work(
            self.bob,
            "project-one",
            "issue",
            claim_id,
            {"expected_version": 1},
            "schema-claim-take-0001",
        )
        self.service.change_work_status(
            self.bob,
            "project-one",
            "issue",
            claim_id,
            {"status": "active", "expected_version": 2},
            "schema-claim-active-0001",
        )
        self.service.change_work_status(
            self.bob,
            "project-one",
            "issue",
            claim_id,
            {"status": "under_review", "expected_version": 3},
            "schema-claim-submit-0001",
        )
        review = self.service.review_work(
            self.alice,
            "project-one",
            "issue",
            claim_id,
            {
                "action": "approved",
                "expected_version": 4,
                "session_id": session_id,
                "body": "Approved.",
            },
            "schema-review-0001",
        ).payload
        self.validate("review-result.schema.json", review)
        self.validate(
            "claim-detail.schema.json",
            self.service.get_work_claim(self.bob, "project-one", "issue", claim_id),
        )

        latest = self.service.get_project_inbox(self.bob, "project-one")[
            "trusted_metadata"
        ]["next_cursor"]
        acknowledgement = self.service.ack_project_inbox(
            self.bob,
            "project-one",
            {"through": latest},
            "schema-inbox-ack-0001",
        ).payload
        self.validate("inbox-ack.schema.json", acknowledgement)
        self.validate(
            "error.schema.json",
            {
                "error": {
                    "code": "version_conflict",
                    "message": "work-claim version has changed",
                    "details": {"expected_version": 3, "current_version": 4},
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
