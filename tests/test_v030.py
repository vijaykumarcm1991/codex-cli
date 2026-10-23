"""Tests for the v0.3.0 upgrade: web tools, AST tools, restore points, audit log,
plan mode, sub-agents, image input, streaming markdown, side-by-side diffs, fuzzy
matching, profiles/presets, stats, session search, HTML export, allowlists, MCP."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from oac import exports, stats
from oac.agent import Agent, Session, estimate, image_content
from oac.api import Model
from oac.audit import AuditLog
from oac.cli import Runner, extract_images, main
from oac.config import PRESETS, Config
from oac.editor import _editor_command
from oac.fuzzy import rank, score
from oac.mcp import McpRegistry, sanitize
from oac.plans import PlanState, parse_plan
from oac.restore import RestorePoints
from oac.subagent import READ_ONLY_SCHEMAS, spawn
from oac.tools import Tools
from oac.ui import UI
from oac.usage import Usage
from oac.webtools import fetch, html_to_text

from test_oac import Handler

SAMPLE_PY = '''"""Sample module."""
import os


def greet(name):
    return f"hello {name}"


class Greeter:
    def loud(self, name):
        return greet(name).upper()
'''


def serve(handler_class):
    """Start a throwaway mock API server; returns (server, base_url with /v1)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/v1"


class V030Case(unittest.TestCase):
    """Temp project + state env; every test gets a clean slate."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"OAC_STATE_DIR": str(self.root / "state"),
                                           "OAC_CONFIG": str(self.root / "config.json")})
        self.env.start()
        self.addCleanup(self.env.stop)


class WebToolTests(V030Case):
    def test_html_to_text_strips_scripts_and_keeps_layout(self):
        html = ("<html><head><title>Docs</title><style>x{}</style></head>"
                "<body><h1>Title</h1><script>alert(1)</script><p>First para</p>"
                "<ul><li>one</li><li>two</li></ul></body></html>")
        title, text = html_to_text(html)
        self.assertEqual(title, "Docs")
        self.assertIn("Title", text)
        self.assertIn("First para", text)
        self.assertIn("- one", text)
        self.assertNotIn("alert", text)

    def test_fetch_reads_local_page(self):
        class Page(Handler):
            def do_GET(self):
                body = b"<html><head><title>Docs</title></head><body><p>Use the tools endpoint.</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server, base = serve(Page)
        self.addCleanup(server.shutdown)
        text = fetch(base + "/docs", max_chars=500)
        self.assertIn("# Docs", text)
        self.assertIn("Use the tools endpoint.", text)

    def test_fetch_rejects_non_http_and_embedded_credentials(self):
        with self.assertRaises(ValueError):
            fetch("ftp://example.com/file")
        with self.assertRaises(ValueError):
            fetch("http://user:pass@127.0.0.1:1/")
        with self.assertRaises(ValueError):
            fetch("not a url")


class AstToolTests(V030Case):
    def test_list_symbols_reports_classes_and_methods(self):
        symbols = exports  # placeholder import guard; real check below
        from oac import asttools
        listed = asttools.list_symbols(SAMPLE_PY)
        names = {s["name"] for s in listed}
        self.assertIn("greet", names)
        self.assertIn("Greeter", names)
        self.assertIn("Greeter.loud", names)
        greet_row = next(s for s in listed if s["name"] == "greet")
        self.assertEqual(greet_row["kind"], "def")

    def test_rename_symbol_updates_definitions_and_references_only(self):
        from oac import asttools
        source = "def total(x):\n    return x\n\n\nclass C:\n    total = staticmethod(total)\n    other = obj.total\n"
        renamed, count = asttools.rename_symbol(source, "total", "sum_all")
        self.assertIn("def sum_all(x):", renamed)
        self.assertIn("staticmethod(sum_all)", renamed)
        self.assertIn("obj.total", renamed)  # attribute access untouched
        self.assertGreaterEqual(count, 3)

    def test_find_references_and_guards(self):
        from oac import asttools
        references = asttools.find_references("value = 1\nvalue = value + 2\nx.value = 3\n", "value")
        lines = [r["line"] for r in references]
        self.assertEqual(lines, [1, 2, 2])
        with self.assertRaises(ValueError):
            asttools.rename_symbol(SAMPLE_PY, "class", "x")  # keyword
        with self.assertRaises(ValueError):
            asttools.rename_symbol(SAMPLE_PY, "missing", "x")

    def test_tools_wire_ast_and_rename(self):
        path = self.root / "mod.py"
        path.write_text(SAMPLE_PY)
        tools = Tools(self.root, approval="auto")
        listing = tools.execute("list_symbols", json.dumps({"path": "mod.py"}))
        self.assertIn("greet", listing)
        result = tools.execute("rename_symbol", json.dumps({"path": "mod.py", "old": "greet", "new": "salute"}))
        self.assertIn("Renamed greet to salute", result)
        self.assertIn("def salute", path.read_text())
        self.assertIn("salute(name)", path.read_text())  # reference updated
        # journal drives /undo for renames too
        self.assertIn("Reverted", tools.undo())


class RestorePointTests(V030Case):
    def test_capture_and_restore_roundtrip(self):
        target = self.root / "cfg.txt"
        target.write_text("version = 1\n")
        points = RestorePoints(self.root)
        points.capture([target], "edit cfg.txt")
        target.write_text("version = 2\n")
        actions = points.restore("1")
        self.assertTrue(any("restored cfg.txt" in a for a in actions))
        self.assertEqual(target.read_text(), "version = 1\n")

    def test_restore_deletes_agent_created_files(self):
        points = RestorePoints(self.root)
        points.capture([self.root / "new.txt"], "write_file: new.txt")  # does not exist yet
        (self.root / "new.txt").write_text("created")
        actions = points.restore("1")
        self.assertFalse((self.root / "new.txt").exists())
        self.assertIn("deleted new.txt", actions[0])

    def test_tools_write_creates_checkpoint(self):
        tools = Tools(self.root, approval="auto")
        tools.execute("write_file", json.dumps({"path": "hello.txt", "content": "v1\n"}))
        tools.execute("edit_file", json.dumps({"path": "hello.txt", "old": "v1", "new": "v2"}))
        listing = RestorePoints(self.root).list()
        self.assertEqual(len(listing), 2)
        RestorePoints(self.root).restore("2")  # snapshot taken before the v1 -> v2 edit
        self.assertEqual((self.root / "hello.txt").read_text(), "v1\n")
        RestorePoints(self.root).restore("1")  # file was agent-created at checkpoint 1
        self.assertFalse((self.root / "hello.txt").exists())


class AuditTests(V030Case):
    def test_log_and_tail(self):
        log = AuditLog(self.root, "sess1")
        log.log("write_file", {"path": "a.txt", "content": "x" * 500}, "Updated a.txt")
        log.log("shell", {"command": "ls"}, "Tool error: nope")
        entries = AuditLog.tail(self.root / ".oac" / "audit.jsonl", 10)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["tool"], "write_file")
        self.assertTrue(entries[0]["ok"])
        self.assertFalse(entries[1]["ok"])
        self.assertLess(len(entries[0]["args"]["content"]), 410)  # truncated

    def test_disabled_writes_nothing(self):
        AuditLog(self.root, "s", enabled=False).log("shell", {}, "ok")
        self.assertFalse((self.root / ".oac" / "audit.jsonl").exists())


class FuzzyTests(unittest.TestCase):
    def test_score_requires_subsequence(self):
        self.assertIsNone(score("zqx", "/help"))
        self.assertIsNotNone(score("/pl", "/plan"))
        self.assertGreater(score("/plan", "/plan approve"), 0)

    def test_rank_orders_best_first(self):
        results = rank("/pln", ["/plan", "/help", "/compact", "/plan approve"])
        self.assertEqual(results[0][1], "/plan")
        names = [name for _score, name in results]
        self.assertNotIn("/help", names)
        self.assertNotIn("/compact", names)
        self.assertIn("/plan approve", names)


class PlanTests(unittest.TestCase):
    def test_parse_fenced_plan(self):
        answer = "Sure!\n\n```plan\n1. Read the config module\n2. Add the flag\n3. Run tests\n```\n"
        self.assertEqual(parse_plan(answer), ["Read the config module", "Add the flag", "Run tests"])

    def test_parse_numbered_fallback_and_cap(self):
        answer = "1. one\n2. two\n" + "\n".join(f"{i}. step {i}" for i in range(3, 20))
        steps = parse_plan(answer)
        self.assertEqual(steps[0], "one")
        self.assertEqual(len(steps), 12)  # capped

    def test_plan_state_lifecycle(self):
        plan = PlanState()
        self.assertEqual(plan.status, "off")
        plan.set_plan(["a", "b"])
        self.assertEqual(plan.status, "review")
        self.assertTrue(plan.start())
        self.assertEqual(plan.pending(), [0, 1])
        plan.mark(0, True)
        self.assertEqual(plan.pending(), [1])
        plan.abort()
        self.assertEqual(plan.status, "off")


class StatsTests(V030Case):
    def test_record_load_and_day_filter(self):
        stats.record("m1", 100, 50, "s1", "/proj")
        stats.record("m2", 10, 5, "s2", "/proj")
        entries = stats.load()
        self.assertEqual(len(entries), 2)
        self.assertEqual(stats.load(days=0), [])  # cutoff excludes today
        board = stats.dashboard(entries)
        self.assertIn("m1", board)
        self.assertIn("110", board)  # 100+10 prompt tokens total

    def test_dashboard_empty(self):
        self.assertIn("No recorded usage yet", stats.dashboard([]))


class ExportTests(unittest.TestCase):
    def test_session_to_html_escapes_and_renders_code(self):
        data = {"id": "abc", "title": "demo <session>", "model": "test",
                "messages": [
                    {"role": "user", "content": "hi <script>alert(1)</script>"},
                    {"role": "assistant", "content": "code:\n```python\nprint(1)\n```"},
                    {"role": "tool", "tool_call_id": "c", "content": "result text"},
                ]}
        html = exports.session_to_html(data, "test")
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("demo &lt;session&gt;", html)
        self.assertIn("<pre><code", html)
        self.assertIn("print(1)", html)
        self.assertIn("result text", html)


class ConfigV030Tests(V030Case):
    def test_profile_roundtrip_and_apply(self):
        config = Config(base_url="http://localhost:9000/v1", api_key_env="K", model="m1")
        config.save_profile("work")
        loaded = Config.load("work")
        self.assertEqual(loaded.base_url, "http://localhost:9000/v1")
        self.assertEqual(loaded.model, "m1")
        with self.assertRaises(ValueError):
            Config.load("nope")

    def test_presets_cover_local_servers(self):
        for name in ("ollama", "lmstudio", "vllm", "jan", "llamacpp"):
            self.assertIn(name, PRESETS)
            self.assertTrue(PRESETS[name]["base_url"].startswith("http://localhost:"))

    def test_new_fields_validate(self):
        with self.assertRaises(ValueError):
            Config(notify="loud").validate()
        with self.assertRaises(ValueError):
            Config(mcp_servers={"bad": {"args": ["x"]}}).validate()  # no command
        Config(notify="desktop", auto_compact=0.5, allow=["git", "pytest"]).validate()


class ToolsV030Tests(V030Case):
    def test_shell_allowlist_prefix_matching(self):
        tools = Tools(self.root, approval="ask", approve=lambda description: False,
                      allow=["echo"])
        result = tools.execute("shell", json.dumps({"command": "echo allowlisted"}))
        self.assertIn("allowlisted", result)
        self.assertIn("[exit 0]", result)
        # Prefix matching: 'echox' does not match the 'echo' prefix
        denied = tools.execute("shell", json.dumps({"command": "echox evil"}))
        self.assertTrue(denied.startswith("Denied by approval"))
        denied = tools.execute("shell", json.dumps({"command": "rm -rf ."}))
        self.assertTrue(denied.startswith("Denied by approval"))

    def test_run_tests_safe_args_and_execution(self):
        tools = Tools(self.root, approval="ask", approve=lambda description: False,
                      test_cmd=f'"{sys.executable}" -c "print(\'tests passed\')"')
        blocked = tools.execute("run_tests", json.dumps({"args": "x; rm -rf /"}))
        self.assertTrue(blocked.startswith("Tool error"))
        result = tools.execute("run_tests", json.dumps({}))
        self.assertIn("tests passed", result)
        self.assertIn("[exit 0]", result)
        read_only = Tools(self.root, approval="read-only", test_cmd="echo nope")
        self.assertTrue(read_only.execute("run_tests", "{}").startswith("Denied"))

    def test_web_fetch_tool_wired(self):
        tools = Tools(self.root)
        with patch("oac.tools.web_fetch", lambda url, max_chars=24000: f"fetched:{url}"):
            result = tools.execute("web_fetch", json.dumps({"url": "https://example.com"}))
        self.assertEqual(result, "fetched:https://example.com")

    def test_audit_hook_records_tool_calls(self):
        log = AuditLog(self.root, "s1")
        tools = Tools(self.root, approval="auto", audit=log)
        tools.execute("list_files", "{}")
        tools.execute("shell", json.dumps({"command": "false"}))
        entries = AuditLog.tail(self.root / ".oac" / "audit.jsonl", 5)
        self.assertEqual([e["tool"] for e in entries], ["list_files", "shell"])


class ImageInputTests(V030Case):
    def test_image_content_builds_data_url(self):
        png = self.root / "pic.png"
        png.write_bytes(b"\x89PNG fake")
        message = image_content("what is this?", [png])
        parts = message["content"]
        self.assertEqual(message["role"], "user")
        self.assertEqual(parts[0]["type"], "text")
        self.assertIn("pic.png", parts[0]["text"])
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        import base64
        self.assertEqual(base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1]), b"\x89PNG fake")

    def test_image_limits_and_types(self):
        paths = []
        for i in range(5):
            p = self.root / f"i{i}.png"
            p.write_bytes(b"x")
            paths.append(p)
        with self.assertRaises(ValueError):
            image_content("x", paths)
        (self.root / "doc.pdf").write_bytes(b"x")
        with self.assertRaises(ValueError):
            image_content("x", [self.root / "doc.pdf"])

    def test_extract_images_from_prompt(self):
        (self.root / "shot.png").write_bytes(b"img")
        clean, images = extract_images("look at @shot.png please", self.root)
        self.assertEqual(clean, "look at please")
        self.assertEqual(images, [self.root / "shot.png"])
        with self.assertRaises(ValueError):
            extract_images("see @missing.png", self.root)
        clean, images = extract_images("email me at bob@example.com", self.root)
        self.assertEqual(images, [])
        self.assertIn("bob@example.com", clean)


class StreamingRenderTests(unittest.TestCase):
    def test_safe_split_boundaries(self):
        self.assertEqual(Runner._safe_split("Para one.\n\nPara two"), 11)
        self.assertEqual(Runner._safe_split("```python\nprint(1)\n"), 0)  # open fence
        split = Runner._safe_split("```python\nprint(1)\n```\nnext")
        self.assertEqual(split, len("```python\nprint(1)\n```\n"))

    def test_streamed_markdown_prints_blocks_progressively(self):
        ui = UI("mono", level=0, width=100)
        runner = Runner(ui, render=True, quiet=False)
        runner.spinner.enabled = False
        out = io.StringIO()
        chunks = ["# Title\n\n", "First para", "graph ends.\n\n", "```python\nprint(", "1)\n", "```\n"]
        with patch("sys.stdout", out):
            runner.emit(chunks[0] + chunks[1] + chunks[2])
            self.assertIn("First paragraph", out.getvalue())
            runner.emit(chunks[3])
            self.assertNotIn("print(", out.getvalue())  # fence still open
            runner.emit(chunks[4] + chunks[5])
            self.assertIn("print(1)", out.getvalue())
            runner.finish("".join(chunks))
        self.assertNotIn("Title", "".join(chunks)[:1])  # sanity: no mutation
        self.assertIn("Title", out.getvalue())

    def test_narrow_terminal_side_by_side_falls_back(self):
        ui = UI("mono", level=0, width=40)
        self.assertIn("too narrow", ui.side_by_side(["a"], ["b"]).lower())


class SideBySideTests(unittest.TestCase):
    def test_side_by_side_shows_both_versions(self):
        ui = UI("mono", level=0, width=100)
        view = ui.side_by_side(["alpha", "beta", "gamma"], ["alpha", "BETA!", "gamma"])
        self.assertIn("before", view)
        self.assertIn("after", view)
        self.assertIn("beta", view)
        self.assertIn("BETA!", view)


class SubAgentTests(V030Case):
    def test_subagent_is_read_only(self):
        class FakeClient:
            def __init__(self):
                self.config = Config()
                self.usage = Usage()
                self.tool_sets = []

            def complete(self, model, messages, tools, on_text=None):
                self.tool_sets.append([t["function"]["name"] for t in tools])
                if len(self.tool_sets) == 1:  # first turn: try to write
                    return {"role": "assistant", "content": None,
                            "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": "write_file",
                                                         "arguments": json.dumps({"path": "evil.txt", "content": "no"})}}]}
                return {"role": "assistant", "content": "found 3 files, no writes possible"}

        client = FakeClient()
        result = spawn(client, Model("m", 8192), self.root, ["survey the project"])
        self.assertIn("found 3 files", result)
        self.assertFalse((self.root / "evil.txt").exists())
        # Only read tools are offered to sub-agents; writes are denied even if forced.
        self.assertNotIn("write_file", client.tool_sets[0])
        self.assertNotIn("shell", client.tool_sets[0])
        self.assertIn("read_file", client.tool_sets[0])

    def test_spawn_validates_prompts(self):
        class Boom:
            def complete(self, *a, **k):
                raise AssertionError("should not be called")

        with self.assertRaises(ValueError):
            spawn(Boom(), Model("m"), self.root, ["", "   "])

    def test_read_only_schema_excludes_mutations(self):
        names = {spec["function"]["name"] for spec in READ_ONLY_SCHEMAS}
        self.assertEqual(names, {"read_file", "list_files", "search", "find_files", "regex_search"})


class AutoCompactTests(V030Case):
    def test_autocompact_replaces_earlier_turns_with_summary(self):
        class FakeClient:
            def __init__(self):
                self.config = Config(max_output_tokens=100)
                self.usage = Usage()
                self.seen = []

            def complete(self, model, messages, tools, on_text=None):
                self.seen.append(messages)
                if len(self.seen) == 1:
                    return {"role": "assistant", "content": "ok"}
                return {"role": "assistant", "content": "GOAL: fix bug; CHANGED: a.py; NEXT: run tests"}

        session = Session(self.root)
        filler = "x" * 250
        for i in range(4):
            session.messages += [{"role": "user", "content": f"turn {i} {filler}"},
                                 {"role": "assistant", "content": f"did {i}"}]
        client = FakeClient()
        agent = Agent(client, Model("m", 2500, 100), Tools(self.root, "read-only"), session,
                      lambda text: None, lambda text: None, stream=False, auto_compact=0.3)
        agent.run("final short prompt")
        first = session.messages[0]["content"]
        self.assertTrue(first.startswith("(Automatic context summary"), first)
        self.assertIn("GOAL: fix bug", first)
        self.assertEqual(session.messages[1]["content"], "final short prompt")  # latest turn kept
        self.assertEqual(session.messages[-1]["content"], "ok")  # and its answer


class McpTests(V030Case):
    FAKE_SERVER = r'''
import json, sys
TOOLS = [
    {"name": "read_note", "description": "Read a note", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, "annotations": {"readOnlyHint": True}},
    {"name": "save_note", "description": "Save a note", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "text": {"type": "string"}}}},
]
for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "called " + msg["params"]["name"]}], "isError": False}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''

    def setUp(self):
        super().setUp()
        self.script = self.root / "fake_mcp.py"
        self.script.write_text(self.FAKE_SERVER)
        self.registry = McpRegistry({"notes": {"command": sys.executable, "args": [str(self.script)]}}, self.root)
        self.addCleanup(self.registry.stop_all)
        self.registry.connect()

    def test_handshake_lists_and_calls_tools(self):
        specs = self.registry.specs()
        names = {spec["function"]["name"] for spec in specs}
        self.assertEqual(names, {"mcp__notes__read_note", "mcp__notes__save_note"})
        read_only = {spec["function"]["name"] for spec in specs
                     if "read_note" in spec["function"]["name"]}
        self.assertIn("read_note", "".join(names))
        result = self.registry.dispatch("mcp__notes__save_note", {"name": "n", "text": "hi"})
        self.assertEqual(result, "called save_note")
        lines = "\n".join(self.registry.status_lines())
        self.assertIn("notes", lines)
        self.assertIn("read_note", lines)

    def test_read_only_annotation_flags_tools(self):
        found = self.registry.lookup("mcp__notes__read_note")
        self.assertIsNotNone(found)
        _server, tool = found
        self.assertTrue(tool.read_only)

    def test_sanitize_and_unknown_tool(self):
        self.assertEqual(sanitize("weird name/with:chars!"), "weird_name_with_chars")
        with self.assertRaises(Exception):
            self.registry.dispatch("mcp__notes__nope", {})


class MiscV030Tests(V030Case):
    def test_editor_command_priority(self):
        with patch.dict(os.environ, {"OAC_EDITOR": "oacedit", "VISUAL": "v", "EDITOR": "e"}):
            self.assertEqual(_editor_command(), "oacedit")
        with patch.dict(os.environ, {"VISUAL": "v", "EDITOR": "e"}, clear=False):
            os.environ.pop("OAC_EDITOR", None)
            self.assertEqual(_editor_command(), "v")

    def test_sanitize_truncates_long_names(self):
        self.assertLessEqual(len(sanitize("x" * 200)), 64)


class CliE2ETests(V030Case):
    """REPL and subcommand end-to-end tests against mock servers."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = serve(Handler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def run_chat(self, commands, *cli):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=[*commands, EOFError]), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(["chat", *cli, "--base-url", self.base, "--cwd", str(self.root),
                         "--approval", "auto", "--json"])
        self.assertEqual(code, 0)
        return stdout.getvalue(), stderr.getvalue()

    def test_export_command_writes_html(self):
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            stdout, _ = self.run_chat(["/export", "/exit"], "Create hello.txt")
        finally:
            os.chdir(cwd)
        session_id = Session.latest(self.root)
        path = self.root / f"oac-{session_id}.html"
        self.assertTrue(path.exists())
        self.assertIn("<!doctype html>", path.read_text())

    def test_restore_command_reverts_agent_file(self):
        stdout, _ = self.run_chat(["/restore 1", "/exit"], "Create hello.txt")
        self.assertFalse((self.root / "hello.txt").exists())
        self.assertIn("hello.txt", stdout)

    def test_fork_command_branches_session(self):
        stdout, _ = self.run_chat(["second prompt", "/fork 1", "/exit"], "Create hello.txt")
        # Find the fork by its recorded parent, independent of mtime ordering.
        sessions = list((self.root / "state" / "sessions").glob("*.json"))
        self.assertEqual(len(sessions), 2)
        data = None
        for path in sessions:
            record = json.loads(path.read_text())
            if "parent" in record:
                data = record
        self.assertIsNotNone(data, "no forked session with a parent field was saved")
        self.assertTrue(data["title"].startswith("fork:"))
        roles = [m["role"] for m in data["messages"]]
        self.assertLessEqual(roles.count("user"), 1)  # only turn 1 kept

    def test_notify_and_mcp_status_commands(self):
        stdout, stderr = self.run_chat(["/notify", "/mcp", "/exit"], "Create hello.txt")
        self.assertIn("notify", stderr)
        self.assertIn("MCP", stdout)

    def test_search_command_finds_sessions(self):
        self.run_chat(["/exit"], "Create hello.txt")
        stdout, _ = capture(lambda: main(["search", "hello.txt"]))
        self.assertIn("Create hello.txt", stdout)

    def test_stats_command(self):
        stats.record("test-coder", 120, 30, "s", "/p")
        stdout, _ = capture(lambda: main(["stats"]))
        self.assertIn("test-coder", stdout)
        stdout, _ = capture(lambda: main(["stats", "--json"]))
        self.assertEqual(len(json.loads(stdout)), 1)

    def test_fetch_command(self):
        class Page(Handler):
            def do_GET(self):
                body = b"<html><body><p>docs body text</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server, base = serve(Page)
        self.addCleanup(server.shutdown)
        stdout, _ = capture(lambda: main(["fetch", base + "/x"]))
        self.assertIn("docs body text", stdout)

    def test_profiles_and_preset_init(self):
        stdout, _ = capture(lambda: main(["init", "--base-url", self.base, "--api-key-env", "K", "--save-as", "work"]))
        self.assertIn("Saved profile", stdout)
        stdout, _ = capture(lambda: main(["profiles"]))
        self.assertIn("work", stdout)
        loaded = Config.load("work")
        self.assertEqual(loaded.base_url, self.base)
        stdout, _ = capture(lambda: main(["init", "--preset", "ollama"]))
        self.assertEqual(Config.load().base_url, PRESETS["ollama"]["base_url"])

    def test_sessions_export_flag(self):
        self.run_chat(["/exit"], "Create hello.txt")
        session_id = Session.latest(self.root)
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            stdout, _ = capture(lambda: main(["sessions", "--export", session_id]))
        finally:
            os.chdir(cwd)
        self.assertIn("Exported", stdout)
        self.assertTrue((self.root / f"oac-{session_id}.html").exists())

    def test_audit_command_lists_tool_calls(self):
        self.run_chat(["/exit"], "Create hello.txt")
        log = self.root / ".oac" / "audit.jsonl"
        self.assertTrue(log.exists())
        stdout, _ = capture(lambda: main(["audit", "--cwd", str(self.root)]))
        self.assertIn("write_file", stdout)

    def test_allow_flag_auto_approves_matching_shell(self):
        class EchoHandler(Handler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                messages = data["messages"]
                if any(m["role"] == "tool" for m in messages):
                    result = next(m["content"] for m in messages if m["role"] == "tool")
                    self.reply({"choices": [{"message": {"role": "assistant", "content": "RESULT:" + result.splitlines()[0]}}]})
                else:
                    self.reply({"choices": [{"message": {"role": "assistant", "content": None,
                                       "tool_calls": [{"id": "c1", "type": "function",
                                                       "function": {"name": "shell", "arguments": json.dumps({"command": "echo allowlist-works"})}}]}}]})

        server, base = serve(EchoHandler)
        self.addCleanup(server.shutdown)
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = main(["run", "run the echo", "--allow", "echo", "--approval", "ask",
                         "--base-url", base, "--cwd", str(self.root), "--json"])
        self.assertEqual(code, 0)
        self.assertIn("allowlist-works", out.getvalue())
        # Without --allow the same command is denied in non-interactive mode.
        out2 = io.StringIO()
        with patch("sys.stdout", out2):
            main(["run", "run the echo", "--approval", "ask", "--base-url", base,
                  "--cwd", str(self.root), "--json"])
        self.assertIn("Denied by approval", out2.getvalue())

    def test_plan_mode_drafts_and_executes(self):
        class PlanHandler(Handler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                messages = data["messages"]
                system = str(messages[0].get("content", ""))
                last = messages[-1].get("content") or ""
                if "Execute step" in last:
                    self.reply({"choices": [{"message": {"role": "assistant", "content": "step finished"}}]})
                elif "PLAN MODE" in system:
                    self.reply({"choices": [{"message": {"role": "assistant",
                                                         "content": "```plan\n1. Survey files\n2. Write the module\n```"}}]})
                else:
                    self.reply({"choices": [{"message": {"role": "assistant", "content": "plain reply"}}]})

        server, base = serve(PlanHandler)
        self.addCleanup(server.shutdown)
        stdout = io.StringIO()
        stderr = io.StringIO()
        commands = ["/plan", "build the feature", "/plan approve", "/exit"]
        with patch("builtins.input", side_effect=[*commands, EOFError]), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(["chat", "--base-url", base, "--cwd", str(self.root), "--approval", "auto", "--json"])
        self.assertEqual(code, 0)
        out = stdout.getvalue() + stderr.getvalue()
        self.assertIn("Proposed plan", out)
        self.assertIn("Survey files", out)
        self.assertIn("☑", out)  # both steps completed
        self.assertIn("plan complete", out)


class BareInvocationTests(V030Case):
    """`oac` with no subcommand defaults to chat; plain text is a chat prompt."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = serve(Handler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def chat(self, argv, commands=("/exit",)):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=[*commands, EOFError]), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main([*argv, "--base-url", self.base, "--cwd", str(self.root),
                         "--approval", "auto", "--json"])
        self.assertEqual(code, 0)
        return stdout.getvalue() + stderr.getvalue()

    def test_bare_oac_opens_chat_repl(self):
        out = self.chat([], commands=("Reply with OK.", "/exit"))
        self.assertIn("OK", out)
        self.assertIn("Session:", out)

    def test_global_flag_without_subcommand_opens_chat(self):
        out = self.chat(["--no-color"], commands=("Reply with OK.", "/exit"))
        self.assertIn("OK", out)

    def test_profile_flag_without_subcommand_opens_chat(self):
        Config(base_url=self.base, api_key_env="K", model="auto").save_profile("work")
        out = self.chat(["--profile", "work"], commands=("Reply with OK.", "/exit"))
        self.assertIn("OK", out)

    def test_plain_text_is_treated_as_chat_prompt(self):
        out = self.chat(["Reply", "with", "OK."])
        self.assertIn("OK", out)

    def test_subcommands_are_untouched(self):
        from oac.cli import _with_default_command
        self.assertEqual(_with_default_command([]), ["chat"])
        self.assertEqual(_with_default_command(["models"]), ["models"])
        self.assertEqual(_with_default_command(["run", "hi"]), ["run", "hi"])
        self.assertEqual(_with_default_command(["fix", "the", "bug"]), ["chat", "fix", "the", "bug"])
        self.assertEqual(_with_default_command(["--no-color"]), ["--no-color", "chat"])
        self.assertEqual(_with_default_command(["--profile", "x"]), ["--profile", "x", "chat"])
        self.assertEqual(_with_default_command(["--profile", "x", "models"]), ["--profile", "x", "models"])
        self.assertEqual(_with_default_command(["--version"]), ["--version"])
        self.assertEqual(_with_default_command(["--no-color", "--json"]),
                         ["--no-color", "chat", "--json"])


def capture(action):
    out, err = io.StringIO(), io.StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        action()
    return out.getvalue(), err.getvalue()


if __name__ == "__main__":
    unittest.main()
