from __future__ import annotations

import io
import json
import sqlite3
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from agent_memory.mcp_server import MAX_QUERY_LENGTH, handle_request, run_server


def request(identifier: int, method: str, params=None):  # type: ignore[no-untyped-def]
    message = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        message["params"] = params
    return message


class McpValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SimpleNamespace(default_results=5)

    def assert_error_code(self, response, code: int) -> None:  # type: ignore[no-untyped-def]
        self.assertIsNotNone(response)
        self.assertEqual(response["error"]["code"], code)

    def test_request_and_params_must_have_json_rpc_shapes(self) -> None:
        self.assert_error_code(handle_request(self.config, []), -32600)
        self.assert_error_code(
            handle_request(self.config, {"id": 1, "method": "ping", "params": {}}),
            -32600,
        )
        self.assert_error_code(
            handle_request(self.config, {"jsonrpc": "2.0", "id": 1, "method": 7}),
            -32600,
        )
        invalid_id = handle_request(
            self.config,
            {"jsonrpc": "2.0", "id": {"invalid": True}, "method": "ping"},
        )
        self.assert_error_code(invalid_id, -32600)
        self.assertIsNone(invalid_id["id"])
        for bad_params in (None, [], "bad", 7, True):
            message = request(2, "tools/call", {})
            message["params"] = bad_params
            self.assert_error_code(handle_request(self.config, message), -32602)

    def test_arguments_must_be_an_object(self) -> None:
        for bad_arguments in (None, [], "bad", 7, True):
            response = handle_request(
                self.config,
                request(
                    1,
                    "tools/call",
                    {"name": "memory_search", "arguments": bad_arguments},
                ),
            )
            self.assert_error_code(response, -32602)

    def test_tool_strings_integer_bounds_and_extra_arguments_are_validated(self) -> None:
        invalid_calls = [
            ("memory_search", {"query": 123}),
            ("memory_search", {"query": "   "}),
            ("memory_search", {"query": "x" * (MAX_QUERY_LENGTH + 1)}),
            ("memory_search", {"query": "valid", "collection": 123}),
            ("memory_search", {"query": "valid", "limit": "5"}),
            ("memory_search", {"query": "valid", "limit": True}),
            ("memory_search", {"query": "valid", "limit": 0}),
            ("memory_search", {"query": "valid", "limit": 21}),
            ("memory_search", {"query": "valid", "unexpected": 1}),
            ("memory_fetch", {"ref": 123}),
            ("memory_fetch", {"ref": "opaque", "max_tokens": "1200"}),
            ("memory_fetch", {"ref": "opaque", "max_tokens": False}),
            ("memory_fetch", {"ref": "opaque", "max_tokens": 127}),
            ("memory_fetch", {"ref": "opaque", "max_tokens": 8001}),
            ("memory_fetch", {"ref": "opaque", "around": -1}),
            ("memory_fetch", {"ref": "opaque", "around": 3}),
            ("memory_fetch", {"ref": "opaque", "part": 0}),
            ("memory_fetch", {"ref": "opaque", "part": True}),
            ("unknown", {}),
        ]
        with patch("agent_memory.mcp_server.search") as mocked_search, patch(
            "agent_memory.mcp_server.fetch"
        ) as mocked_fetch:
            for index, (name, arguments) in enumerate(invalid_calls, 1):
                with self.subTest(name=name, arguments=arguments):
                    response = handle_request(
                        self.config,
                        request(
                            index,
                            "tools/call",
                            {"name": name, "arguments": arguments},
                        ),
                    )
                    self.assert_error_code(response, -32602)
            mocked_search.assert_not_called()
            mocked_fetch.assert_not_called()

    def test_valid_integer_arguments_are_passed_without_coercion(self) -> None:
        with patch("agent_memory.mcp_server.search", return_value=[]) as mocked_search:
            response = handle_request(
                self.config,
                request(
                    1,
                    "tools/call",
                    {
                        "name": "memory_search",
                        "arguments": {"query": "  학습 설계  ", "collection": "  vault ", "limit": 3},
                    },
                ),
            )
        self.assertIn("result", response)
        mocked_search.assert_called_once_with(
            self.config,
            "학습 설계",
            limit=3,
            collection="vault",
        )


class McpProcessBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SimpleNamespace(default_results=5)

    def run_lines(self, messages):  # type: ignore[no-untyped-def]
        stdin = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(sys, "stdin", stdin), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = run_server(self.config)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        return exit_code, responses, stderr.getvalue()

    def test_sqlite_failure_returns_internal_error_and_next_request_runs(self) -> None:
        messages = [
            request(
                1,
                "tools/call",
                {"name": "memory_search", "arguments": {"query": "database failure"}},
            ),
            request(2, "ping", {}),
        ]
        with patch(
            "agent_memory.mcp_server.search",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            exit_code, responses, diagnostics = self.run_lines(messages)

        self.assertEqual(exit_code, 0)
        self.assertEqual([item["id"] for item in responses], [1, 2])
        self.assertEqual(responses[0]["error"]["code"], -32603)
        self.assertIn("result", responses[1])
        self.assertIn("database error", diagnostics)

    def test_unexpected_failure_returns_internal_error_and_next_request_runs(self) -> None:
        messages = [
            request(1, "tools/call", {"name": "memory_catalog", "arguments": {}}),
            request(2, "ping", {}),
        ]
        with patch("agent_memory.mcp_server.catalog", side_effect=RuntimeError("unexpected")):
            exit_code, responses, diagnostics = self.run_lines(messages)

        self.assertEqual(exit_code, 0)
        self.assertEqual([item["id"] for item in responses], [1, 2])
        self.assertEqual(responses[0]["error"]["code"], -32603)
        self.assertIn("result", responses[1])
        self.assertIn("internal error", diagnostics)

    def test_invalid_top_level_value_does_not_kill_server(self) -> None:
        exit_code, responses, _diagnostics = self.run_lines(
            [
                ["not", "an", "object"],
                request(2, "ping", {}),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertIn("result", responses[1])


if __name__ == "__main__":
    unittest.main()
