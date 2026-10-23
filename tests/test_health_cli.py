from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from imitation_krab import __version__
from imitation_krab.cli import main
from imitation_krab.config import MAX_REQUEST_BYTES


class HealthCommandTests(unittest.TestCase):
    def run_health(self, *, body=b'{"status":"ok"}', version=__version__,
                   status=200, failure=None, human=False, server=None,
                   global_json=False):
        output, error = StringIO(), StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = ["--db", str(root / "state" / "db"),
                    "--pepper-file", str(root / "state" / "pepper"),
                    "--token-file", str(root / "missing-token")]
            if server is not None:
                argv.extend(["--server", server])
            if global_json:
                argv.extend(["--json", "health"])
            else:
                argv.extend(["health"] if human else ["health", "--json"])
            with (
                patch("imitation_krab.cli.http.client.HTTPConnection") as connection,
                patch("imitation_krab.cli.Database") as database,
                patch("imitation_krab.cli._read_token") as read_token,
                redirect_stdout(output), redirect_stderr(error),
            ):
                response = connection.return_value.getresponse.return_value
                response.status = status
                response.read.return_value = body
                response.getheader.return_value = version
                connection.return_value.getresponse.side_effect = failure
                result = main(argv)
                database.assert_not_called()
                read_token.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])
        return result, output.getvalue(), error.getvalue(), connection

    def test_success_without_credentials_or_local_state(self):
        for version in (__version__, "0.7.0-beta+1", None):
            with self.subTest(version=version), patch.dict(
                os.environ, {"KRAB_SERVER": "http://localhost:9876"}
            ):
                result, output, error, connection = self.run_health(version=version)
                self.assertEqual(result, 0, error)
                self.assertEqual(json.loads(output), {
                    "status": "ok", "server_version": version,
                })
                self.assertEqual(connection.call_args.args, ("localhost", 9876))
                request = connection.return_value.request.call_args
                self.assertEqual(request.args, ("GET", "/v1/health"))
                self.assertNotIn("Authorization", request.kwargs["headers"])
                connection.return_value.close.assert_called_once_with()

    def test_connection_has_five_second_timeout(self):
        result, _, error, connection = self.run_health()
        self.assertEqual(result, 0, error)
        self.assertEqual(connection.call_args.kwargs.get("timeout"), 5)

    def test_response_read_is_bounded(self):
        result, _, error, connection = self.run_health()
        self.assertEqual(result, 0, error)
        response = connection.return_value.getresponse.return_value
        response.read.assert_called_once_with(MAX_REQUEST_BYTES + 1)

    def test_human_output_and_explicit_server(self):
        result, output, error, connection = self.run_health(
            human=True, server="http://127.0.0.1:9877"
        )
        self.assertEqual(result, 0, error)
        self.assertEqual(output.splitlines(), [f"ok (server {__version__})"])
        self.assertEqual(connection.call_args.args, ("127.0.0.1", 9877))

    def test_global_json_and_version_header_not_body(self):
        result, output, error, _ = self.run_health(
            body=b'{"status":"ok","server_version":"untrusted"}',
            global_json=True,
        )
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output), {
            "status": "ok", "server_version": __version__,
        })

    def test_legacy_human_output(self):
        result, output, error, _ = self.run_health(human=True, version=None)
        self.assertEqual(result, 0, error)
        self.assertEqual(output, "ok (server unknown)\n")

    def test_failures_are_safe(self):
        cases = [
            {"failure": ConnectionRefusedError("refused")},
            {"failure": TimeoutError("slow")},
            {"status": 503}, {"status": 302},
            {"body": b"not-json"}, {"body": b"\xff"},
            {"body": b"[]"}, {"body": b"{}"},
            {"body": b'{"status":"down"}'},
            {"body": b'{"status":"ok\\n\\u001b[31m"}'},
            {"version": "evil\n\x1b[31m"}, {"version": ""},
            {"version": "a" * 65},
            {"body": b" " * (MAX_REQUEST_BYTES + 1)},
        ]
        for case in cases:
            with self.subTest(case=repr(case)[:100]):
                result, output, error, connection = self.run_health(**case)
                self.assertEqual(result, 1)
                self.assertEqual(output, "")
                self.assertIn("error:", error)
                self.assertNotIn("\x1b", error)
                connection.return_value.close.assert_called_once_with()

    def test_rejects_non_loopback_server(self):
        result, output, error, connection = self.run_health(server="http://example.com")
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertIn("loopback", error)
        connection.assert_not_called()
