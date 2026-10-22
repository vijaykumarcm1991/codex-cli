import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from oac.agent import Agent, Session, fit_context
from oac.api import APIError, Client, Model, metadata, sse
from oac.cli import main
from oac.config import Config
from oac.tools import Tools


class Handler(BaseHTTPRequestHandler):
    requests = []
    model_records = [{"id": "test-coder", "context_length": 32768, "max_output_tokens": 4096}]

    def log_message(self, *args):
        pass

    def reply(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        if self.path == "/v1/models":
            self.reply({"data": self.model_records})
        elif self.path.startswith("/v1/models/"):
            self.reply({"id": "test-coder", "limits": {"context_window": 16384}})
        else:
            self.reply({"error": "missing"}, 404)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, data))
        if data.get("model") == "legacy" and "max_completion_tokens" in data:
            self.reply({"error": "unsupported parameter: max_completion_tokens"}, 400)
            return
        if self.path == "/v1/responses":
            self.reply({"output": [{"type": "message", "content": [{"type": "output_text", "text": "Responses OK"}]}]})
            return
        messages = data["messages"]
        if messages[-1].get("content") == "Reply with OK.":
            self.reply({"choices": [{"message": {"role": "assistant", "content": "OK"}}]})
            return
        done = any(m["role"] == "tool" for m in messages)
        message = {"role": "assistant", "content": "File created and verified." if done else None}
        if not done:
            message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "hello.txt", "content": "hello world\n"})}}]
        if not data.get("stream"):
            self.reply({"choices": [{"message": message}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if done:
            deltas = [{"content": "File created "}, {"content": "and verified."}]
        else:
            fn = message["tool_calls"][0]["function"]
            deltas = [{"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": fn["name"], "arguments": fn["arguments"][:15]}}]}, {"tool_calls": [{"index": 0, "function": {"arguments": fn["arguments"][15:]}}]}]
        for delta in deltas:
            self.wfile.write(("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"OAC_STATE_DIR": str(self.root / "state"), "OAC_CONFIG": str(self.root / "config.json")})
        self.env.start()
        self.addCleanup(self.env.stop)
        Handler.requests = []

    def test_streaming_agent_executes_tool_and_persists_resumable_session(self):
        client = Client(Config(base_url=self.base))
        model = client.discover()
        self.assertEqual(model.context_window, 32768)
        self.assertEqual(model.source, "API metadata")
        session = Session(self.root)
        output = []
        agent = Agent(client, model, Tools(self.root, "auto"), session, output.append, lambda text: None)
        self.assertEqual(agent.run("Create hello.txt"), "File created and verified.")
        self.assertEqual((self.root / "hello.txt").read_text(), "hello world\n")
        self.assertEqual("".join(output), "File created and verified.")
        restored = Session(self.root, session.id)
        self.assertEqual(restored.messages, session.messages)
        self.assertEqual([m["role"] for m in restored.messages], ["user", "assistant", "tool", "assistant"])

    def test_cli_noninteractive_json(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", io.StringIO()):
            code = main(["run", "Create a file", "--base-url", self.base, "--cwd", str(self.root), "--approval", "auto", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["answer"], "File created and verified.")

    def test_responses_api_conversion(self):
        client = Client(Config(base_url=self.base, api_mode="responses"))
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}, {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a"}'}}]}, {"role": "tool", "tool_call_id": "c1", "content": "data"}]
        result = client.complete(Model("test"), messages, [])
        self.assertEqual(result["content"], "Responses OK")
        path, body = Handler.requests[-1]
        self.assertEqual(path, "/v1/responses")
        self.assertFalse(body["store"])
        self.assertEqual(body["input"][-1]["type"], "function_call_output")

    def test_model_detail_enrichment_and_override(self):
        with patch.object(Handler, "model_records", [{"id": "test-coder"}]):
            model = Client(Config(base_url=self.base)).discover()
            self.assertEqual(model.context_window, 16384)
            model = Client(Config(base_url=self.base, context_window=20000)).discover()
            self.assertEqual(model.context_window, 20000)
            self.assertEqual(model.source, "configured override")

    def test_unknown_context_is_not_fabricated(self):
        client = Client(Config(base_url=self.base, model="custom"))
        with patch.object(client, "models", return_value=[]), patch.object(client, "request", side_effect=APIError("404")):
            self.assertIsNone(client.discover().context_window)

    def test_legacy_max_tokens_retry(self):
        result = Client(Config(base_url=self.base)).complete(Model("legacy"), [{"role": "user", "content": "hello"}], [])
        self.assertIn("tool_calls", result)
        self.assertEqual(len(Handler.requests), 2)
        self.assertIn("max_completion_tokens", Handler.requests[0][1])
        self.assertIn("max_tokens", Handler.requests[1][1])
        self.assertNotIn("max_completion_tokens", Handler.requests[1][1])

    def test_config_roundtrip_without_persisting_key(self):
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "do-not-save-this"}):
            config = Config(base_url=self.base, api_key_env="TEST_PROVIDER_KEY", context_window=20000)
            config.save()
            self.assertEqual(Config.load().api_key_env, "TEST_PROVIDER_KEY")
            self.assertNotIn("do-not-save-this", (self.root / "config.json").read_text())
            self.assertEqual((self.root / "config.json").stat().st_mode & 0o777, 0o600)

    def test_resume_rejects_different_root_and_path_traversal(self):
        session = Session(self.root)
        session.save("test")
        with self.assertRaises(ValueError):
            Session(self.root / "other", session.id)
        with self.assertRaises(ValueError):
            Session(self.root, "../escape")


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = Tools(self.root, "auto")

    def call(self, name, **args):
        return self.tools.execute(name, json.dumps(args))

    def test_read_write_edit_search(self):
        self.call("write_file", path="nested/file.txt", content="one\ntwo\n")
        self.assertIn("2: two", self.call("read_file", path="nested/file.txt"))
        self.assertIn("Updated", self.call("edit_file", path="nested/file.txt", old="two", new="three"))
        self.assertIn("nested/file.txt:2: three", self.call("search", query="three"))
        self.assertIn("nested/file.txt", self.call("list_files"))
        self.assertIn("exactly once", self.call("edit_file", path="nested/file.txt", old="missing", new="no"))

    def test_paths_and_symlinks_confined(self):
        self.assertIn("escapes", self.call("read_file", path="../outside"))
        (self.root / "link").symlink_to("/etc/passwd")
        self.assertIn("escapes", self.call("read_file", path="link"))
        self.assertIn("protected", self.call("read_file", path=".env"))
        self.assertIn("protected", self.call("write_file", path=".git/config", content="bad"))

    def test_default_and_read_only_deny_mutation(self):
        for policy in ("ask", "read-only"):
            tools = Tools(self.root, policy)
            self.assertIn("Denied", tools.execute("write_file", '{"path":"a","content":"b"}'))
            self.assertFalse((self.root / "a").exists())
            self.assertIn("Denied", tools.execute("shell", '{"command":"touch bad"}'))
            self.assertFalse((self.root / "bad").exists())

    def test_shell_exit_and_timeout(self):
        self.assertIn("[exit 3]", self.call("shell", command="printf hello; exit 3"))
        self.assertIn("terminated", self.call("shell", command="sleep 10", timeout=1))

    def test_long_shell_output_preserves_exit_status(self):
        result = self.call("shell", command="python3 -c 'print(\"x\" * 30000)'; exit 7")
        self.assertIn("truncated", result)
        self.assertTrue(result.endswith("[exit 7]"))

    def test_approval_detects_concurrent_edit(self):
        (self.root / "a").write_text("original")
        def approval(description):
            (self.root / "a").write_text("user edit")
            return True
        tools = Tools(self.root, "ask", approval)
        self.assertIn("changed during approval", tools.execute("write_file", '{"path":"a","content":"agent edit"}'))
        self.assertEqual((self.root / "a").read_text(), "user edit")


class ContextTests(unittest.TestCase):
    def test_trim_preserves_tool_pairs_and_latest_turn(self):
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "x" * 8000}, {"role": "assistant", "tool_calls": []}, {"role": "tool", "content": "old"}, {"role": "user", "content": "latest"}]
        result, removed = fit_context(messages, 2000, 200)
        self.assertEqual(removed, 3)
        self.assertEqual([m["role"] for m in result], ["system", "user"])
        self.assertEqual(result[-1]["content"], "latest")

    def test_oversize_current_turn_fails_instead_of_corrupting_history(self):
        with self.assertRaises(APIError):
            fit_context([{"role": "user", "content": "x" * 8000}], 1000, 100)

    def test_metadata_variants(self):
        self.assertEqual(metadata({"id": "x", "top_provider": {"context_length": "128000"}}).context_window, 128000)
        self.assertIsNone(metadata({"id": "x", "context_length": True}).context_window)

    def test_sse_comments_and_multiline_data(self):
        events = list(sse(io.BytesIO(b': keepalive\n\ndata: {"a":\ndata: 1}\n\ndata: [DONE]\n\n')))
        self.assertEqual(events, [{"a": 1}])

    def test_remote_http_disallowed(self):
        with self.assertRaises(ValueError):
            Client(Config(base_url="http://example.com/v1"))


if __name__ == "__main__":
    unittest.main()
