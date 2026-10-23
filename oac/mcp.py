"""Model Context Protocol (MCP) client: stdio transport, JSON-RPC 2.0.

oac connects to MCP servers configured under ``mcp_servers`` (command + args),
performs the initialize handshake, lists their tools, and bridges them into the
agent tool loop as ``mcp__<server>__<tool>`` functions. Pure standard library.
"""
from __future__ import annotations

import json
import os
import re
import select
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
START_TIMEOUT = 20.0
CALL_TIMEOUT = 60.0


class McpError(RuntimeError):
    pass


def sanitize(name: str) -> str:
    """Make a tool name safe for OpenAI function-calling (<=64 chars)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_") or "tool"
    return cleaned[:64]


@dataclass
class RemoteTool:
    server: str
    name: str
    schema: dict  # OpenAI function schema
    read_only: bool = False


class McpServer:
    """One spawned MCP server process speaking line-delimited JSON-RPC."""

    def __init__(self, name: str, spec: dict, root: Path):
        self.name = name
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            raise McpError(f"mcp server {name!r} needs a non-empty 'command'")
        self.command = [command, *[str(a) for a in spec.get("args", [])]]
        self.env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        self.root = Path(root).resolve()
        self.proc: subprocess.Popen | None = None
        self._stderr = tempfile.TemporaryFile()
        self._id = 0
        self._buffer = b""
        self.tools: dict[str, RemoteTool] = {}
        self.error = ""

    # -- transport ---------------------------------------------------------

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _write(self, payload: dict) -> None:
        if not self.proc or self.proc.poll() is not None:
            raise McpError(f"MCP server {self.name} is not running")
        try:
            self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError) as exc:
            raise McpError(f"MCP server {self.name} write failed: {exc}") from None

    def _read_line(self, deadline: float) -> bytes:
        """Read one newline-terminated chunk from stdout with a timeout."""
        stream = self.proc.stdout
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(f"MCP server {self.name} timed out")
            ready, _, _ = select.select([stream], [], [], min(remaining, 0.5))
            if not ready:
                if self.proc.poll() is not None:
                    raise McpError(f"MCP server {self.name} exited (code {self.proc.returncode}): {self.stderr_tail()}")
                continue
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                if self.proc.poll() is not None:
                    raise McpError(f"MCP server {self.name} closed the connection: {self.stderr_tail()}")
                time.sleep(0.02)
                continue
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line

    def _request(self, method: str, params: dict | None = None, timeout: float = CALL_TIMEOUT) -> dict:
        message_id = self._next_id()
        payload = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self._read_line(deadline)
            except McpError:
                raise
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue  # non-JSON noise on stdout
            if message.get("id") != message_id:
                continue  # a notification or someone else's response
            if message.get("error"):
                raise McpError(f"MCP {self.name}.{method}: {message['error'].get('message', message['error'])}")
            return message.get("result") or {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        try:
            self.proc = subprocess.Popen(
                self.command, cwd=self.root, shell=False,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                env={**os.environ, **self.env}, start_new_session=True)
        except OSError as exc:
            raise McpError(f"Cannot start MCP server {self.name}: {exc}") from None
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "oac", "version": "0.3.0"},
        }, timeout=START_TIMEOUT)
        if str(result.get("protocolVersion", "")) != PROTOCOL_VERSION:
            # Tolerate servers that negotiate a different draft; log it in status.
            self.negotiated = str(result.get("protocolVersion", "?"))
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.refresh_tools()

    negotiated: str = PROTOCOL_VERSION

    def refresh_tools(self) -> None:
        result = self._request("tools/list", {}, timeout=START_TIMEOUT)
        self.tools = {}
        for tool in result.get("tools", []):
            name = str(tool.get("name", "")).strip()
            if not name:
                continue
            schema = tool.get("inputSchema") or {}
            properties = schema.get("properties") if isinstance(schema, dict) else None
            spec = {
                "type": "function",
                "function": {
                    "name": sanitize(f"mcp__{self.name}__{name}"),
                    "description": (str(tool.get("description") or f"MCP tool {name!r} on server {self.name!r}").strip())[:600],
                    "parameters": {"type": "object",
                                   "properties": properties if isinstance(properties, dict) else {},
                                   "required": [str(r) for r in (schema.get("required") or [])] if isinstance(schema, dict) else [],
                                   "additionalProperties": False},
                },
            }
            annotations = tool.get("annotations") or {}
            read_only = bool(annotations.get("readOnlyHint"))
            self.tools[name] = RemoteTool(self.name, name, spec, read_only)

    def call(self, tool: str, arguments: dict, timeout: float = CALL_TIMEOUT) -> str:
        result = self._request("tools/call", {"name": tool, "arguments": arguments}, timeout=timeout)
        if result.get("isError"):
            return f"MCP tool error: {_content_text(result) or 'unknown error'}"
        text = _content_text(result)
        return text or "(empty result)"
    def stderr_tail(self) -> str:
        try:
            self._stderr.seek(0)
            return self._stderr.read(2000).decode(errors="replace").strip()[-400:]
        except (OSError, ValueError):
            return ""

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, 15)
                    self.proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        self.proc.kill()
                    except OSError:
                        pass
        if self.proc:
            for stream in (self.proc.stdout,):
                try:
                    stream.close()
                except OSError:
                    pass
        self.proc = None
        try:
            self._stderr.close()
        except OSError:
            pass


def _content_text(result: dict) -> str:
    parts = []
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif isinstance(item, dict) and item.get("type") == "resource":
            resource = item.get("resource") or {}
            parts.append(str(resource.get("text", "")))
    return "\n".join(parts)


class McpRegistry:
    """All configured MCP servers, bridged into the agent tool loop."""

    def __init__(self, servers: dict, root: Path):
        self.root = Path(root).resolve()
        self.servers: dict[str, McpServer] = {}
        self.failures: dict[str, str] = {}
        for name, spec in (servers or {}).items():
            if not isinstance(spec, dict):
                self.failures[str(name)] = "configuration must be an object"
                continue
            try:
                self.servers[str(name)] = McpServer(str(name), spec, self.root)
            except McpError as exc:
                self.failures[str(name)] = str(exc)

    def connect(self, status=None) -> None:
        """Start every server; failures are recorded, never fatal."""
        for name, server in self.servers.items():
            if status:
                status(f"mcp {name}: connecting...")
            try:
                server.start()
                if status:
                    status(f"mcp {name}: {len(server.tools)} tools")
            except (McpError, OSError) as exc:
                server.stop()
                self.servers.pop(name, None)
                self.failures[name] = str(exc)
                if status:
                    status(f"mcp {name}: failed — {exc}")

    def specs(self) -> list[dict]:
        return [tool.schema for server in self.servers.values() for tool in server.tools.values()]

    def lookup(self, sanitized_name: str) -> tuple[McpServer, RemoteTool] | None:
        for server in self.servers.values():
            for tool in server.tools.values():
                if tool.schema["function"]["name"] == sanitized_name:
                    return server, tool
        return None

    def dispatch(self, sanitized_name: str, arguments: dict) -> str:
        found = self.lookup(sanitized_name)
        if not found:
            raise McpError(f"Unknown MCP tool {sanitized_name}")
        server, tool = found
        try:
            return server.call(tool.name, arguments)
        except McpError as exc:
            # One transparent restart, then surface the error to the model.
            server.stop()
            try:
                server.start()
                return server.call(tool.name, arguments)
            except (McpError, OSError) as retry_exc:
                self.servers.pop(server.name, None)
                self.failures[server.name] = str(retry_exc)
                raise McpError(str(exc)) from None

    def status_lines(self) -> list[str]:
        lines = []
        for name, server in sorted(self.servers.items()):
            tools = ", ".join(sorted(server.tools)) or "no tools"
            lines.append(f"{name}: {len(server.tools)} tools ({tools[:100]}{'...' if len(tools) > 100 else ''})")
        for name, error in sorted(self.failures.items()):
            lines.append(f"{name}: unavailable — {error}")
        if not lines:
            lines.append("no MCP servers configured (add one under mcp_servers in the config)")
        return lines

    def stop_all(self) -> None:
        for server in self.servers.values():
            try:
                server.stop()
            except OSError:
                pass
