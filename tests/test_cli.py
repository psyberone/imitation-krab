from __future__ import annotations

import json
import os
import runpy
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from imitation_krab.cli import (
    APIClient,
    ClientError,
    _emit,
    _read_claim_manifest,
    _read_token,
    _run_client,
    _safe_error,
    _write_token,
    build_parser,
    main,
)
from imitation_krab.config import CONTAINER_HOST, DEFAULT_HOST, MAX_REQUEST_BYTES


TOKEN = "krab_usr_" + "a" * 32 + "_" + "b" * 43
CLAIM_ID = "clm_" + "c" * 32
SESSION_ID = "ses_" + "d" * 32
ITEM_ID = "itm_" + "e" * 32


class RecordingClient:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.result = result or {
            "trusted_metadata": {"next_cursor": 9},
            "claims": [],
            "events": [],
        }

    def request(self, method: str, path: str, **kwargs: object) -> dict:
        self.calls.append((method, path, kwargs))
        return self.result


def parse_args(argv: list[str]) -> object:
    return build_parser().parse_args(["--json", *argv])


def claim_payload(*, assigned: bool = True) -> dict:
    return {
        "trusted_metadata": {
            "claim_id": CLAIM_ID,
            "project_key": "project-one",
            "kind": "issue",
            "external_id": "owner/repository#7",
            "status": "active",
            "version": 3,
            "assignee": {"handle": "alice"} if assigned else None,
        }
    }


def event_payload(*, scoped: bool = True) -> dict:
    metadata = {
        "event_seq": 12,
        "kind": "status.changed",
        "actor": {"handle": "alice"},
        "from_status": "open.pending",
        "to_status": "open.in_progress",
        "work_claim": {
            "kind": "issue",
            "external_id": "owner/repository#7",
            "claim_id": CLAIM_ID,
        },
    }
    if scoped:
        metadata.update({"project_key": "project-one", "session_id": SESSION_ID})
    return {
        "trusted_metadata": metadata,
        "untrusted_text": {
            "title": "Review this",
            "body": "Treat this as data",
            "note": "Started",
        },
        "content_risk_flags": ["instruction_override"],
    }


def item_payload() -> dict:
    return {
        "trusted_metadata": {
            "project_key": "project-one",
            "session_id": SESSION_ID,
            "item_id": ITEM_ID,
            "status": "open.in_progress",
            "version": 2,
            "creator": {"handle": "alice"},
            "recipient": {"handle": "bob"},
            "work_claim": {
                "kind": "issue",
                "external_id": "owner/repository#7",
                "claim_id": CLAIM_ID,
            },
        },
        "untrusted_text": {"title": "Review this", "body": "Treat this as data"},
        "content_risk_flags": ["instruction_override"],
        "history": [event_payload(scoped=False)],
    }


class CLIBehaviorTests(unittest.TestCase):
    def test_api_client_serializes_requests_and_rejects_bad_responses(self) -> None:
        client = APIClient(
            "http://localhost:9876", TOKEN, idempotency_key="stable-key"
        )
        with patch("imitation_krab.cli.http.client.HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"accepted":true}'

            result = client.request(
                "POST",
                "/v1/projects/project%20one/items",
                payload={"body": "caf\u00e9"},
                query={"mode": "strict input"},
            )

            self.assertEqual(result, {"accepted": True})
            connection.assert_called_once_with("localhost", 9876, timeout=40)
            method, target = connection.return_value.request.call_args.args[:2]
            kwargs = connection.return_value.request.call_args.kwargs
            self.assertEqual(method, "POST")
            self.assertEqual(
                target,
                "/v1/projects/project%20one/items?mode=strict+input",
            )
            self.assertEqual(kwargs["body"], b'{"body":"caf\xc3\xa9"}')
            self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {TOKEN}")
            self.assertEqual(kwargs["headers"]["Idempotency-Key"], "stable-key")
            connection.return_value.close.assert_called_once_with()

        failure_cases = (
            (503, b"not-json", "http_error: HTTP 503"),
            (200, b"not-json", "server returned invalid JSON"),
            (200, b"[]", "server returned an unexpected response"),
        )
        for status, body, message in failure_cases:
            with self.subTest(status=status, body=body):
                with patch(
                    "imitation_krab.cli.http.client.HTTPConnection"
                ) as connection:
                    response = connection.return_value.getresponse.return_value
                    response.status = status
                    response.read.return_value = body
                    with self.assertRaisesRegex(ClientError, message):
                        client.request("GET", "/v1/projects")
                    connection.return_value.close.assert_called_once_with()

        with patch("imitation_krab.cli.http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.side_effect = TimeoutError("slow")
            with self.assertRaisesRegex(ClientError, "server unavailable: slow"):
                client.request("GET", "/v1/projects")
            connection.return_value.close.assert_called_once_with()

        with self.assertRaisesRegex(ClientError, "invalid port"):
            APIClient("http://localhost:not-a-port", TOKEN)

    def test_every_client_command_maps_to_an_exact_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body_file = root / "body.txt"
            body_file.write_text("From a file", encoding="utf-8")
            manifest = root / "claims.json"
            manifest.write_text('["owner/repository#8"]', encoding="utf-8")
            project = "project/one"
            base = f"/v1/projects/project%2Fone/sessions/{SESSION_ID}"
            claim_base = "/v1/projects/project%2Fone/issues"
            cases = (
                (["projects"], ("GET", "/v1/projects", {})),
                (
                    ["issues", "--project", project],
                    ("GET", claim_base, {"query": None}),
                ),
                (
                    [
                        "claim-add",
                        "owner/repository#8",
                        "--project",
                        project,
                        "--kind",
                        "issue",
                    ],
                    (
                        "POST",
                        claim_base,
                        {"payload": {"external_id": "owner/repository#8"}},
                    ),
                ),
                (
                    [
                        "claim-import",
                        str(manifest),
                        "--project",
                        project,
                        "--kind",
                        "issue",
                    ],
                    (
                        "POST",
                        claim_base + "/import",
                        {"payload": {"external_ids": ["owner/repository#8"]}},
                    ),
                ),
                (
                    [
                        "claim",
                        CLAIM_ID,
                        "--project",
                        project,
                        "--kind",
                        "issue",
                        "--expected-version",
                        "1",
                    ],
                    (
                        "POST",
                        f"{claim_base}/{CLAIM_ID}/claim",
                        {"payload": {"expected_version": 1}},
                    ),
                ),
                (
                    [
                        "claim-assign",
                        CLAIM_ID,
                        "--project",
                        project,
                        "--kind",
                        "issue",
                        "--to",
                        "bob",
                        "--expected-version",
                        "2",
                    ],
                    (
                        "POST",
                        f"{claim_base}/{CLAIM_ID}/assign",
                        {"payload": {"assignee": "bob", "expected_version": 2}},
                    ),
                ),
                (
                    [
                        "claim-release",
                        CLAIM_ID,
                        "--project",
                        project,
                        "--kind",
                        "issue",
                        "--expected-version",
                        "3",
                    ],
                    (
                        "POST",
                        f"{claim_base}/{CLAIM_ID}/release",
                        {"payload": {"expected_version": 3}},
                    ),
                ),
                (
                    [
                        "claim-status",
                        CLAIM_ID,
                        "active",
                        "--project",
                        project,
                        "--kind",
                        "issue",
                        "--expected-version",
                        "4",
                    ],
                    (
                        "PATCH",
                        f"{claim_base}/{CLAIM_ID}/status",
                        {"payload": {"status": "active", "expected_version": 4}},
                    ),
                ),
                (
                    [
                        "claim-review",
                        CLAIM_ID,
                        "changes-requested",
                        "--project",
                        project,
                        "--kind",
                        "issue",
                        "--session",
                        SESSION_ID,
                        "--expected-version",
                        "5",
                        "--body-file",
                        str(body_file),
                    ],
                    (
                        "POST",
                        f"{claim_base}/{CLAIM_ID}/review",
                        {
                            "payload": {
                                "action": "changes_requested",
                                "expected_version": 5,
                                "session_id": SESSION_ID,
                                "body": "From a file",
                            }
                        },
                    ),
                ),
                (
                    [
                        "claim-show",
                        CLAIM_ID,
                        "--project",
                        project,
                        "--kind",
                        "issue",
                    ],
                    ("GET", f"{claim_base}/{CLAIM_ID}", {}),
                ),
                (
                    ["sessions", "--project", project],
                    ("GET", "/v1/projects/project%2Fone/sessions", {}),
                ),
                (
                    [
                        "session-create",
                        "--project",
                        project,
                        "--label",
                        "Sprint",
                        "--participant",
                        "alice",
                        "--participant",
                        "bob",
                    ],
                    (
                        "POST",
                        "/v1/projects/project%2Fone/sessions",
                        {"payload": {"label": "Sprint", "participants": ["alice", "bob"]}},
                    ),
                ),
                (
                    [
                        "session-close",
                        "--project",
                        project,
                        "--session",
                        SESSION_ID,
                    ],
                    ("POST", base + "/close", {"payload": {}}),
                ),
                (
                    [
                        "send",
                        "--project",
                        project,
                        "--session",
                        SESSION_ID,
                        "--to",
                        "bob",
                        "--title",
                        "Review",
                        "--body",
                        "Please review",
                        "--claim",
                        CLAIM_ID,
                    ],
                    (
                        "POST",
                        base + "/items",
                        {
                            "payload": {
                                "to": "bob",
                                "title": "Review",
                                "body": "Please review",
                                "claim_id": CLAIM_ID,
                            }
                        },
                    ),
                ),
                (
                    [
                        "queue",
                        "--project",
                        project,
                        "--session",
                        SESSION_ID,
                        "--after",
                        "7",
                        "--limit",
                        "8",
                        "--wait",
                        "9",
                    ],
                    (
                        "GET",
                        base + "/queue",
                        {"query": {"after": 7, "limit": 8, "wait": 9}},
                    ),
                ),
                (
                    [
                        "inbox",
                        "--project",
                        project,
                        "--after",
                        "6",
                        "--limit",
                        "7",
                        "--wait",
                        "8",
                    ],
                    (
                        "GET",
                        "/v1/projects/project%2Fone/inbox",
                        {"query": {"after": 6, "limit": 7, "wait": 8}},
                    ),
                ),
                (
                    ["inbox-ack", "--project", project, "--through", "22"],
                    (
                        "POST",
                        "/v1/projects/project%2Fone/inbox/ack",
                        {"payload": {"through": 22}},
                    ),
                ),
                (
                    [
                        "show",
                        "--project",
                        project,
                        "--session",
                        SESSION_ID,
                        ITEM_ID,
                    ],
                    ("GET", base + f"/items/{ITEM_ID}", {}),
                ),
                (
                    [
                        "status",
                        "--project",
                        project,
                        "--session",
                        SESSION_ID,
                        ITEM_ID,
                        "approved",
                        "--expected-version",
                        "2",
                        "--note-file",
                        str(body_file),
                    ],
                    (
                        "PATCH",
                        base + f"/items/{ITEM_ID}/status",
                        {
                            "payload": {
                                "status": "open.approved",
                                "expected_version": 2,
                                "note": "From a file",
                            }
                        },
                    ),
                ),
                (
                    [
                        "notify",
                        "--project",
                        project,
                        "--session",
                        SESSION_ID,
                        ITEM_ID,
                    ],
                    ("POST", base + f"/items/{ITEM_ID}/notify", {"payload": {}}),
                ),
            )

            for argv, expected in cases:
                with self.subTest(command=argv[0]):
                    client = RecordingClient()
                    output = StringIO()
                    with redirect_stdout(output):
                        result = _run_client(parse_args(argv), client)  # type: ignore[arg-type]
                    self.assertEqual(result, 0)
                    self.assertEqual(client.calls, [expected])
                    self.assertIsInstance(json.loads(output.getvalue()), dict)

        with self.assertRaisesRegex(ValueError, "unknown command"):
            _run_client(SimpleNamespace(command="unknown"), RecordingClient())  # type: ignore[arg-type]

    def test_claim_import_dry_run_and_manifest_validation(self) -> None:
        client = RecordingClient(
            {
                "claims": [
                    {"trusted_metadata": {"external_id": "owner/repository#1"}}
                ]
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "claims.json"
            manifest.write_text(
                '["owner/repository#1","owner/repository#2"]', encoding="utf-8"
            )
            output = StringIO()
            args = parse_args(
                [
                    "claim-import",
                    str(manifest),
                    "--project",
                    "project-one",
                    "--kind",
                    "pr",
                    "--dry-run",
                ]
            )
            with redirect_stdout(output):
                self.assertEqual(_run_client(args, client), 0)  # type: ignore[arg-type]
            result = json.loads(output.getvalue())
            self.assertEqual(result["would_create"], ["owner/repository#2"])
            self.assertEqual(result["trusted_metadata"]["unchanged_count"], 1)
            self.assertEqual(
                client.calls,
                [("GET", "/v1/projects/project-one/pull-requests", {})],
            )

            oversized = Path(directory) / "large.json"
            oversized.write_bytes(b"[" + b" " * MAX_REQUEST_BYTES + b"]")
            with self.assertRaisesRegex(ClientError, "exceeds"):
                _read_claim_manifest(str(oversized))

            invalid = Path(directory) / "invalid.json"
            for raw in (b"\xff", b"[1.5]", b"[NaN]", b"["):
                with self.subTest(raw=raw):
                    invalid.write_bytes(raw)
                    with self.assertRaisesRegex(ClientError, "valid UTF-8 JSON"):
                        _read_claim_manifest(str(invalid))

        stdin = SimpleNamespace(buffer=BytesIO(b'["owner/repository#3"]'))
        with patch("imitation_krab.cli.sys.stdin", stdin):
            self.assertEqual(_read_claim_manifest("-"), ["owner/repository#3"])

    def test_admin_cli_lifecycle_and_failure_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "krab.db"
            token_file = root / "owner.token"
            backup = root / "backup.db"

            def invoke(*arguments: str) -> tuple[int, str, str]:
                stdout = StringIO()
                stderr = StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    result = main(["--json", "--db", str(database), *arguments])
                return result, stdout.getvalue(), stderr.getvalue()

            result, output, _ = invoke("init")
            self.assertEqual(result, 0)
            self.assertTrue(json.loads(output)["initialized"])

            result, output, _ = invoke(
                "admin", "user-create", "owner", "--write-token", str(token_file)
            )
            self.assertEqual(result, 0)
            created = json.loads(output)
            self.assertEqual(created["credential_delivery"], "file")
            self.assertNotIn("token", created)
            original_token = _read_token(token_file)

            result, output, _ = invoke("admin", "user-create", "dev")
            self.assertEqual(result, 0)
            self.assertRegex(json.loads(output)["token"], r"^krab_usr_")

            self.assertEqual(
                invoke("admin", "project-create", "project-one", "--label", "One")[
                    0
                ],
                0,
            )
            self.assertEqual(
                invoke(
                    "admin",
                    "project-add-user",
                    "project-one",
                    "owner",
                    "--role",
                    "admin",
                )[0],
                0,
            )
            self.assertEqual(
                invoke(
                    "admin",
                    "project-add-user",
                    "project-one",
                    "dev",
                    "--role",
                    "coordinator",
                )[0],
                0,
            )

            result, output, _ = invoke("admin", "user-list")
            self.assertEqual(result, 0)
            users = json.loads(output)
            self.assertEqual(
                [entry["trusted_metadata"]["handle"] for entry in users["users"]],
                ["dev", "owner"],
            )
            self.assertEqual(
                {entry["trusted_metadata"]["token_version"] for entry in users["users"]},
                {1},
            )

            result, output, _ = invoke("admin", "project-list")
            self.assertEqual(result, 0)
            projects = json.loads(output)
            self.assertEqual(
                projects["projects"][0]["trusted_metadata"]["project_key"],
                "project-one",
            )
            self.assertEqual(
                projects["projects"][0]["untrusted_text"], {"label": "One"}
            )

            result, output, _ = invoke(
                "admin", "project-members", "project-one"
            )
            self.assertEqual(result, 0)
            members = json.loads(output)
            self.assertEqual(members["trusted_metadata"]["project_key"], "project-one")
            self.assertEqual(
                {
                    entry["trusted_metadata"]["handle"]: entry["trusted_metadata"]["role"]
                    for entry in members["members"]
                },
                {"dev": "coordinator", "owner": "admin"},
            )

            def all_keys(value: object) -> set[str]:
                if isinstance(value, dict):
                    return set(value) | set().union(
                        *(all_keys(nested) for nested in value.values())
                    )
                if isinstance(value, list):
                    return set().union(*(all_keys(nested) for nested in value))
                return set()

            for inventory in (users, projects, members):
                self.assertTrue(
                    all_keys(inventory).isdisjoint(
                        {"token", "token_digest", "digest", "bearer_token"}
                    )
                )

            result, output, _ = invoke(
                "admin", "user-rotate", "owner", "--write-token", str(token_file)
            )
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output)["credential_delivery"], "file")
            self.assertNotEqual(_read_token(token_file), original_token)

            result, output, _ = invoke("admin", "backup", str(backup))
            self.assertEqual(result, 0)
            self.assertEqual(Path(json.loads(output)["backup"]), backup)
            self.assertTrue(backup.is_file())

            self.assertEqual(invoke("admin", "user-disable", "dev")[0], 0)

            result, output, _ = invoke("admin", "user-list")
            self.assertEqual(result, 0)
            updated_users = {
                entry["trusted_metadata"]["handle"]: entry["trusted_metadata"]
                for entry in json.loads(output)["users"]
            }
            self.assertEqual(updated_users["owner"]["token_version"], 2)
            self.assertFalse(updated_users["dev"]["active"])

            result, output, _ = invoke(
                "admin", "project-members", "project-one"
            )
            self.assertEqual(result, 0)
            updated_members = {
                entry["trusted_metadata"]["handle"]: entry["trusted_metadata"]
                for entry in json.loads(output)["members"]
            }
            self.assertFalse(updated_members["dev"]["active"])
            self.assertEqual(updated_members["owner"]["token_version"], 2)

            result, _, error = invoke(
                "admin", "project-members", "missing-project"
            )
            self.assertEqual(result, 1)
            self.assertIn("project not found", error)

            result, _, error = invoke("admin", "user-create", "owner")
            self.assertEqual(result, 1)
            self.assertIn("error:", error)
            self.assertIn("already exists", error)

    def test_token_files_rotate_atomically_and_reject_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_file = root / "token"
            _write_token(token_file, TOKEN, replace=False)
            with self.assertRaisesRegex(ClientError, "refusing to overwrite"):
                _write_token(token_file, TOKEN, replace=False)

            replacement = "krab_usr_" + "f" * 32 + "_" + "g" * 43
            _write_token(token_file, replacement, replace=True)
            self.assertEqual(_read_token(token_file), replacement)
            self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)

            missing = root / "missing"
            with self.assertRaisesRegex(ClientError, "not found"):
                _read_token(missing)

            token_file.write_bytes(b"x" * 513)
            os.chmod(token_file, 0o600)
            with self.assertRaisesRegex(ClientError, "unexpectedly large"):
                _read_token(token_file)

            token_file.write_bytes(b"\xff")
            with self.assertRaisesRegex(ClientError, "invalid"):
                _read_token(token_file)

            token_file.write_text("not-a-token", encoding="ascii")
            with self.assertRaisesRegex(ClientError, "does not contain"):
                _read_token(token_file)

            token_file.unlink()
            token_file.mkdir()
            with self.assertRaisesRegex(ClientError, "regular file"):
                _read_token(token_file)
            token_file.rmdir()

            target = root / "target"
            target.write_text(TOKEN, encoding="ascii")
            os.chmod(target, 0o600)
            token_file.symlink_to(target)
            with self.assertRaisesRegex(ClientError, "regular file|symlink"):
                _read_token(token_file)
            with self.assertRaisesRegex(ClientError, "existing token path"):
                _write_token(token_file, replacement, replace=True)
            token_file.unlink()

            os.chmod(root, 0o755)
            with self.assertRaisesRegex(ClientError, "mode 0700"):
                _write_token(token_file, TOKEN, replace=False)
            os.chmod(root, 0o700)

    def test_pretty_output_marks_data_and_formats_every_resource(self) -> None:
        args = SimpleNamespace(json=False)
        outputs: list[str] = []

        def render(payload: dict, kind: str | None = None) -> str:
            output = StringIO()
            with redirect_stdout(output):
                _emit(args, payload, kind=kind)
            rendered = output.getvalue()
            outputs.append(rendered)
            return rendered

        projects = render(
            {
                "projects": [
                    {
                        "trusted_metadata": {
                            "project_key": "project-one",
                            "role": "admin",
                            "project_id": "prj_123",
                        },
                        "untrusted_text": {"label": "Project One"},
                        "content_risk_flags": ["external_url"],
                    }
                ]
            },
            "projects",
        )
        self.assertIn("project-one  [admin]  prj_123", projects)
        self.assertIn("untrusted_text.label", projects)

        sessions = render(
            {
                "sessions": [
                    {
                        "trusted_metadata": {
                            "session_id": SESSION_ID,
                            "state": "active",
                            "created_at": "2026-10-07T00:00:00Z",
                        },
                        "untrusted_text": {"label": "Sprint"},
                        "content_risk_flags": [],
                    }
                ]
            },
            "sessions",
        )
        self.assertIn(f"{SESSION_ID}  [active]", sessions)

        session = render(
            {
                "trusted_metadata": {
                    "project_key": "project-one",
                    "session_id": SESSION_ID,
                    "state": "active",
                },
                "untrusted_text": {"label": "Sprint"},
                "content_risk_flags": [],
            },
            "session",
        )
        self.assertIn(f"[project-one/{SESSION_ID}] active", session)

        claims = render(
            {"claims": [claim_payload(assigned=False), claim_payload()]}, "claims"
        )
        self.assertIn("assignee=-", claims)
        self.assertIn("assignee=alice", claims)

        imported = render(
            {
                "trusted_metadata": {
                    "project_key": "project-one",
                    "kind": "issue",
                    "requested_count": 2,
                    "created_count": 1,
                    "unchanged_count": 1,
                },
                "created": [claim_payload()],
            },
            "claim-import",
        )
        self.assertIn("requested=2 created=1 unchanged=1", imported)

        dry_run = render(
            {
                "trusted_metadata": {
                    "project_key": "project-one",
                    "kind": "issue",
                    "requested_count": 1,
                    "would_create_count": 1,
                    "unchanged_count": 0,
                    "dry_run": True,
                },
                "would_create": ["owner/repository#8"],
            },
            "claim-import",
        )
        self.assertIn("would-create=1", dry_run)
        self.assertIn("would create owner/repository#8", dry_run)

        self.assertIn("owner/repository#7", render(claim_payload(), "claim"))

        review = render(
            {
                "trusted_metadata": {"action": "approved", "review_round": 2},
                "claim": claim_payload(),
                "notification": item_payload(),
            },
            "review",
        )
        self.assertIn("review approved round=2", review)
        self.assertIn("linked issue owner/repository#7", review)

        detail = render(
            {
                "claim": claim_payload(),
                "review_history": [
                    {
                        "trusted_metadata": {
                            "actor": {"handle": "reviewer"},
                            "from_status": "under_review",
                            "to_status": "active",
                            "review_round": 2,
                            "action": "changes_requested",
                            "created_at": "2026-10-07T00:00:00Z",
                        }
                    }
                ],
            },
            "claim-detail",
        )
        self.assertIn("changes_requested by reviewer under_review -> active", detail)

        item = render(item_payload(), "item")
        self.assertIn("CONTENT WARNING: instruction_override", item)
        self.assertIn("untrusted_text.body", item)

        queue = render({"events": [event_payload()]}, "queue")
        self.assertIn(f"[project-one/{SESSION_ID}] event#12", queue)
        self.assertIn("open.pending -> open.in_progress", queue)
        self.assertIn("untrusted_text.note", queue)

        generic = render({"z": 1})
        self.assertEqual(json.loads(generic), {"z": 1})

        error = StringIO()
        with redirect_stderr(error):
            _safe_error("bad\x1b[2J")
        self.assertEqual(error.getvalue(), "error:\n  bad\\u001b[2J\n")
        self.assertGreater(sum(len(output) for output in outputs), 500)

    def test_main_dispatches_server_and_client_and_handles_interrupts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "krab.db"
            with patch("imitation_krab.cli.serve") as run_server:
                self.assertEqual(
                    main(["--db", str(database), "serve", "--port", "9001"]), 0
                )
            run_server.assert_called_once()
            positional, keyword = run_server.call_args
            self.assertEqual(positional[1:], (DEFAULT_HOST, 9001))
            self.assertFalse(keyword["container_bind"])

            with patch(
                "imitation_krab.cli.serve", side_effect=KeyboardInterrupt
            ) as run_server:
                self.assertEqual(
                    main(["--db", str(database), "serve", "--container-bind"]),
                    130,
                )
            self.assertEqual(run_server.call_args.args[1], CONTAINER_HOST)

            fake_client = Mock()
            with (
                patch("imitation_krab.cli._read_token", return_value=TOKEN),
                patch("imitation_krab.cli.APIClient", return_value=fake_client) as api,
                patch("imitation_krab.cli._run_client", return_value=17) as run_client,
            ):
                result = main(
                    [
                        "--db",
                        str(database),
                        "--server",
                        "http://localhost:9999",
                        "--idempotency-key",
                        "retry-me",
                        "projects",
                    ]
                )
            self.assertEqual(result, 17)
            api.assert_called_once_with(
                "http://localhost:9999", TOKEN, idempotency_key="retry-me"
            )
            run_client.assert_called_once_with(run_client.call_args.args[0], fake_client)

            error = StringIO()
            with (
                patch(
                    "imitation_krab.cli._read_token",
                    side_effect=ClientError("unsafe\x1b[2J"),
                ),
                redirect_stderr(error),
            ):
                self.assertEqual(main(["--db", str(database), "projects"]), 1)
            self.assertIn("unsafe\\u001b[2J", error.getvalue())

        with patch("imitation_krab.cli.main", return_value=23):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_module("imitation_krab.__main__", run_name="__main__")
        self.assertEqual(raised.exception.code, 23)


if __name__ == "__main__":
    unittest.main()
