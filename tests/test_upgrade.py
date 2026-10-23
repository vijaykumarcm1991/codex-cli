"""Tests for the v0.2.0 modernization: UI, usage tracking, retries, new tools,
session management, and interactive REPL commands."""
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from oac.agent import Agent, Session
from oac.api import APIError, Client, Model
from oac.cli import main, osc52, transcript
from oac.config import Config
from oac.tools import Tools
from oac.ui import THEMES, DEFAULT_THEME, UI, visible_len, wrap_ansi
from oac.usage import Usage, format_cost

from test_oac import Handler  # reuse the mock API server definition


class ServerTest(unittest.TestCase):
    """Shared mock API server and temp state, without re-running upstream tests."""

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


class UITests(unittest.TestCase):
    def test_theme_registry(self):
        self.assertEqual(DEFAULT_THEME, "tokyo-night")
        self.assertLessEqual({"tokyo-night", "dracula", "nord", "light", "mono"}, set(THEMES))

    def test_markdown_uses_color_and_keeps_text(self):
        ui = UI("tokyo-night", level=3, width=80)
        out = ui.md("# Title\n\nHello **world** and `code`.\n\n- item one\n- item two\n")
        self.assertIn("\x1b[", out)
        self.assertIn("Title", out)
        self.assertIn("world", out)
        self.assertIn("item one", out)

    def test_markdown_plain_when_color_disabled(self):
        ui = UI("tokyo-night", level=0, width=80)
        out = ui.md("# Title\n\nHello **world**.")
        self.assertNotIn("\x1b[", out)
        self.assertIn("Title", out)

    def test_code_block_panel_and_highlight(self):
        ui = UI("tokyo-night", level=3, width=80)
        out = ui.md("```python\ndef hello():\n    return 'x'  # note\n```\n")
        self.assertIn("╭", out)
        self.assertIn("python", out)
        self.assertIn("\x1b[", out)  # syntax colors present

    def test_table_aligns_columns(self):
        ui = UI("tokyo-night", level=0, width=100)
        out = ui.table(["head-a", "head-b"], [["one", "two"], ["1", "2"]])
        lines = out.split("\n")
        self.assertEqual(len({len(line) for line in lines}), 1)

    def test_progress_bar(self):
        ui = UI("tokyo-night", level=3, width=80)
        bar = ui.bar(0.5, width=10)
        self.assertIn("█", bar)
        self.assertIn("░", bar)

    def test_wrap_ansi_respects_visible_width(self):
        ui = UI("tokyo-night", level=3, width=80)
        styled = ui.s("word ", color="#ff0000") + " ".join(["padding"] * 40)
        wrapped = wrap_ansi(styled, 30)
        for line in wrapped.split("\n"):
            self.assertLessEqual(visible_len(line), 30)

    def test_banner_contains_logo_and_version(self):
        ui = UI("tokyo-night", level=3, width=80)
        out = ui.banner("0.2.0", "test-coder", "32k", "/tmp/p", "abc")
        self.assertIn("v0.2.0", out)
        self.assertIn("test-coder", out)
        self.assertIn("▄███▄", out)

    def test_json_highlighting(self):
        ui = UI("tokyo-night", level=3, width=80)
        out = ui.highlight('{"a": 1, "b": true}', "json")
        self.assertIn("\x1b[", out)


class UsageTests(unittest.TestCase):
    def test_totals_and_cost(self):
        usage = Usage()
        usage.add("gpt-4o", 1000, 500)
        usage.add("llama-3", 2000, 1000)
        self.assertEqual(usage.requests, 2)
        self.assertEqual(usage.total_tokens, 4500)
        cost = usage.cost({"gpt-4o": {"input": 5, "output": 15}})
        self.assertAlmostEqual(cost, 1000 / 1e6 * 5 + 500 / 1e6 * 15)
        # Pricing keys match by substring, so gpt-4o-mini also matches gpt-4o.
        usage2 = Usage()
        usage2.add("gpt-4o-mini", 1000, 500)
        self.assertAlmostEqual(usage2.cost({"gpt-4o": {"input": 5, "output": 15}}), 0.0125)
        self.assertEqual(format_cost(0.0125), "$0.0125")
        self.assertEqual(format_cost(3.5), "$3.50")

    def test_no_pricing_match_is_none(self):
        usage = Usage()
        usage.add("mystery-model", 10, 10)
        self.assertIsNone(usage.cost({"gpt-4o": {"input": 5, "output": 15}}))
        self.assertIsNone(Usage().cost({"gpt-4o": {"input": 5, "output": 15}}))

    def test_zero_and_garbage_ignored(self):
        usage = Usage()
        usage.add("m", 0, 0)
        usage.add("m", None, "junk")
        self.assertEqual(usage.requests, 0)


class ToolUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = Tools(self.root, "auto")

    def call(self, name, **args):
        return self.tools.execute(name, json.dumps(args))

    def test_find_files_glob(self):
        self.call("write_file", path="src/deep/mod.py", content="x = 1\n")
        self.call("write_file", path="docs/readme.md", content="# hi\n")
        result = self.call("find_files", pattern="**/*.py")
        self.assertIn("src/deep/mod.py", result)
        self.assertNotIn("docs/readme.md", result)
        self.assertIn("no matches", self.call("find_files", pattern="**/*.rs"))

    def test_find_files_rejects_absolute_pattern(self):
        self.assertIn("Tool error", self.call("find_files", pattern="/etc/*.conf"))

    def test_regex_search(self):
        self.call("write_file", path="app.py", content="value = 42\nname = 'test'\n")
        result = self.call("regex_search", pattern=r"\b\d+\b")
        self.assertIn("app.py:1: value = 42", result)
        result = self.call("regex_search", pattern=r"Name")
        self.assertIn("no matches", result)  # case-sensitive
        self.assertIn("Tool error", self.call("regex_search", pattern="([unclosed"))

    def test_undo_reverts_write(self):
        self.call("write_file", path="file.txt", content="agent version\n")
        self.assertTrue((self.root / "file.txt").exists())
        self.assertIn("Deleted", self.tools.undo())
        self.assertFalse((self.root / "file.txt").exists())
        self.assertIn("Nothing to undo", self.tools.undo())

    def test_undo_restores_previous_content(self):
        (self.root / "cfg.toml").write_text("original\n")
        self.call("edit_file", path="cfg.toml", old="original", new="changed")
        self.assertIn("Reverted", self.tools.undo())
        self.assertEqual((self.root / "cfg.toml").read_text(), "original\n")

    def test_undo_refuses_when_file_changed_afterwards(self):
        self.call("write_file", path="later.txt", content="agent\n")
        (self.root / "later.txt").write_text("user edit\n")
        self.assertIn("Skipped undo", self.tools.undo())
        self.assertEqual((self.root / "later.txt").read_text(), "user edit\n")


class RetryAndUsageAPITests(ServerTest):
    """Runs against fresh mock servers; each test builds its own handler."""

    def test_retry_on_503_then_success(self):
        class Flaky(Handler):
            requests = []
            failures = [1]

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                type(self).requests.append((self.path, body))
                if type(self).failures[0] > 0:
                    type(self).failures[0] -= 1
                    self.reply({"error": "overloaded"}, 503)
                    return
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"choices": [{"delta": {"content": "OK"}}]}\n\n')
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                self.reply({"choices": [{"message": {"role": "assistant", "content": "OK"}}]})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Client(Config(base_url=f"http://127.0.0.1:{server.server_port}/v1", retries=3, retry_backoff=0.01))
            result = client.complete(Model("test"), [{"role": "user", "content": "hi"}], [])
            self.assertEqual(result["content"], "OK")
            self.assertEqual(len(Flaky.requests), 2)  # one failure, one success
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_retries_exhausted_raises(self):
        class AlwaysFailing(Handler):
            requests = []

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                json.loads(self.rfile.read(length)) if length else None
                self.reply({"error": "down"}, 503)

        server = ThreadingHTTPServer(("127.0.0.1", 0), AlwaysFailing)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Client(Config(base_url=f"http://127.0.0.1:{server.server_port}/v1", retries=2, retry_backoff=0.01))
            with self.assertRaises(APIError) as ctx:
                client.complete(Model("test"), [{"role": "user", "content": "hi"}], [])
            self.assertIn("503", str(ctx.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_usage_captured_from_stream_chunk(self):
        class UsageStream(Handler):
            requests = []

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                type(self).requests.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n')
                if body.get("stream_options", {}).get("include_usage"):
                    self.wfile.write(b'data: {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

        server = ThreadingHTTPServer(("127.0.0.1", 0), UsageStream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Client(Config(base_url=f"http://127.0.0.1:{server.server_port}/v1"))
            chunks = []
            result = client.complete(Model("metered"), [{"role": "user", "content": "hi"}], [], chunks.append)
            self.assertEqual(result["content"], "hi")
            self.assertEqual(client.usage.prompt_tokens, 11)
            self.assertEqual(client.usage.completion_tokens, 7)
            # The client asked for usage reporting in the stream options.
            self.assertTrue(UsageStream.requests[0][1].get("stream_options", {}).get("include_usage"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_stream_options_fallback_on_400(self):
        class NoStreamOptions(Handler):
            requests = []

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                type(self).requests.append((self.path, body))
                if "stream_options" in body:
                    self.reply({"error": "unsupported parameter: stream_options"}, 400)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices": [{"delta": {"content": "fine"}}]}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

        server = ThreadingHTTPServer(("127.0.0.1", 0), NoStreamOptions)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Client(Config(base_url=f"http://127.0.0.1:{server.server_port}/v1"))
            result = client.complete(Model("picky"), [{"role": "user", "content": "hi"}], [], lambda text: None)
            self.assertEqual(result["content"], "fine")
            self.assertNotIn("stream_options", NoStreamOptions.requests[1][1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_usage_captured_nonstream(self):
        class NonStreamUsage(Handler):
            requests = []

            def do_POST(self):
                self.reply({"choices": [{"message": {"role": "assistant", "content": "OK"}}],
                            "usage": {"prompt_tokens": 5, "completion_tokens": 3}})

        server = ThreadingHTTPServer(("127.0.0.1", 0), NonStreamUsage)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Client(Config(base_url=f"http://127.0.0.1:{server.server_port}/v1"))
            client.complete(Model("m"), [{"role": "user", "content": "hi"}], [])
            self.assertEqual(client.usage.total_tokens, 8)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class SessionUpgradeTests(ServerTest):
    def test_title_persisted_and_restored(self):
        session = Session(self.root)
        session.title = "fix the tests"
        session.save("test-model")
        data = json.loads(session.path.read_text())
        self.assertEqual(data["title"], "fix the tests")
        restored = Session(self.root, session.id)
        self.assertEqual(restored.title, "fix the tests")

    def test_agent_sets_title_from_first_prompt(self):
        client = Client(Config(base_url=self.base))
        model = client.discover()
        session = Session(self.root)
        agent = Agent(client, model, Tools(self.root, "auto"), session, lambda text: None, lambda text: None)
        agent.run("Create hello.txt and verify it")
        self.assertEqual(session.title, "Create hello.txt and verify it")

    def test_latest_finds_most_recent_session_for_root(self):
        old = Session(self.root)
        old.title = "older"
        old.save("m")
        import time as _time
        _time.sleep(0.02)
        new = Session(self.root)
        new.save("m")
        other_root = self.root / "elsewhere"
        other_root.mkdir()
        elsewhere = Session(other_root)
        elsewhere.save("m")
        self.assertEqual(Session.latest(self.root), new.id)
        self.assertEqual(Session.latest(other_root), elsewhere.id)
        self.assertIsNone(Session.latest(self.root / "missing"))

    def test_cli_resume_continues_session(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", io.StringIO()):
            first = main(["run", "Create a file", "--base-url", self.base, "--cwd", str(self.root), "--approval", "auto", "--json"])
            self.assertEqual(first, 0)
            session_id = json.loads(stdout.getvalue())["session_id"]
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", io.StringIO()):
            code = main(["run", "and check it", "--base-url", self.base, "--cwd", str(self.root),
                         "--approval", "auto", "--json", "--resume", session_id])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["session_id"], session_id)
        # The resumed conversation kept the earlier turn.
        restored = Session(self.root, session_id)
        self.assertEqual(restored.title, "Create a file")

    def test_sessions_json_and_delete(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", io.StringIO()):
            main(["run", "Create a file", "--base-url", self.base, "--cwd", str(self.root), "--approval", "auto", "--json"])
            session_id = json.loads(stdout.getvalue())["session_id"]
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", io.StringIO()):
            code = main(["sessions", "--json"])
        self.assertEqual(code, 0)
        records = json.loads(stdout.getvalue())
        match = [r for r in records if r["id"] == session_id]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["title"], "Create a file")
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            code = main(["sessions", "--delete", session_id])
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "state" / "sessions" / f"{session_id}.json").exists())
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["sessions", "--delete", session_id]), 1)

    def test_cli_continue_resumes_latest(self):
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            main(["run", "Create a file", "--base-url", self.base, "--cwd", str(self.root), "--approval", "auto", "--json"])
        session_id = Session.latest(self.root)
        self.assertTrue(session_id)
        stdout = io.StringIO()
        with patch("builtins.input", side_effect=EOFError), patch("sys.stdout", stdout), patch("sys.stderr", io.StringIO()):
            code = main(["chat", "Check the file", "--base-url", self.base, "--cwd", str(self.root),
                         "--approval", "auto", "--json", "--continue"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["session_id"], session_id)


class ReplCommandTests(ServerTest):
    def run_chat(self, commands, *cli):
        """Drive the chat REPL with scripted input; returns combined stdout."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("builtins.input", side_effect=[*commands, EOFError]), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(["chat", *cli, "--base-url", self.base, "--cwd", str(self.root),
                         "--approval", "auto", "--json"])
        self.assertEqual(code, 0)
        return stdout.getvalue(), stderr.getvalue()

    def test_undo_command_reverts_agent_file(self):
        stdout, _ = self.run_chat(["/undo", "/exit"], "Create hello.txt")
        self.assertIn("hello.txt", stdout)  # answer mentions the file
        self.assertFalse((self.root / "hello.txt").exists())

    def test_help_and_theme_and_unknown(self):
        stdout, stderr = self.run_chat(["/help", "/theme", "/bogus", "/exit"], "Create hello.txt")
        self.assertIn("/undo", stdout)
        self.assertIn("tokyo-night", stdout)
        self.assertIn("unknown command", stderr)

    def test_title_and_save_commands(self):
        cwd = os.getcwd()
        os.chdir(self.root)  # /save defaults to the process working directory
        try:
            stdout, stderr = self.run_chat(["/title fix everything", "/save", "/exit"], "Create hello.txt")
        finally:
            os.chdir(cwd)
        session_id = Session.latest(self.root)
        data = json.loads((self.root / "state" / "sessions" / f"{session_id}.json").read_text())
        self.assertEqual(data["title"], "fix everything")
        saved = Path(self.root / f"oac-{session_id}.md")
        self.assertTrue(saved.exists())
        self.assertIn("## You", saved.read_text())
        self.assertIn("fix everything", transcript(Session(self.root, session_id), "test"))

    def test_usage_panel_after_run(self):
        stdout, stderr = self.run_chat(["/usage", "/exit"], "Create hello.txt")
        # The mock server does not report usage, so the empty-state hint shows.
        self.assertIn("usage", stdout)

    def test_context_bar(self):
        stdout, _ = self.run_chat(["/context", "/exit"], "Create hello.txt")
        self.assertIn("tokens (estimated)", stdout)
        self.assertIn("█", stdout)

    def test_sessions_command_in_repl(self):
        stdout, _ = self.run_chat(["/sessions", "/exit"], "Create hello.txt")
        self.assertIn("Create hello.txt", stdout)


class MiscTests(unittest.TestCase):
    def test_osc52_sequence(self):
        encoded = osc52("hello")
        self.assertTrue(encoded.startswith("\x1b]52;c;"))
        self.assertTrue(encoded.endswith("\x07"))

    def test_theme_command_persists(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        with patch.dict(os.environ, {"OAC_CONFIG": str(root / "config.json"), "OAC_STATE_DIR": str(root / "state")}):
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(["theme", "dracula"]), 0)
            self.assertEqual(Config.load().theme, "dracula")
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(["theme", "not-a-theme"]), 1)

    def test_config_carries_new_defaults(self):
        config = Config()
        self.assertEqual(config.theme, "tokyo-night")
        self.assertEqual(config.retries, 3)
        self.assertEqual(config.pricing, {})


if __name__ == "__main__":
    unittest.main()
