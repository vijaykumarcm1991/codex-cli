from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .config import Config
from .usage import Usage


class APIError(RuntimeError):
    pass


@dataclass
class Model:
    id: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    source: str = "not advertised"


def positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if number > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def metadata(record: dict) -> Model:
    # Providers differ in where they expose limits. Never infer limits from a model name.
    containers = [record]
    for key in ("metadata", "capabilities", "limits", "model_info", "config", "top_provider"):
        if isinstance(record.get(key), dict):
            containers.append(record[key])
    context = output = None
    for container in containers:
        for key in ("context_window", "context_length", "max_context_length", "max_model_len", "max_position_embeddings"):
            context = context or positive_int(container.get(key))
        for key in ("max_output_tokens", "max_completion_tokens"):
            output = output or positive_int(container.get(key))
    return Model(str(record.get("id", "")), context, output, "API metadata" if context else "not advertised")


class Client:
    RETRYABLE = {429, 500, 502, 503, 504}

    def __init__(self, config: Config):
        self.config = config
        self.usage = Usage()
        parsed = urllib.parse.urlsplit(config.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must be an HTTP(S) URL without credentials, query, or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Use HTTPS for remote APIs; HTTP is allowed only for loopback servers")
        self.base = config.base_url.rstrip("/")

    def _backoff(self, attempt: int) -> float:
        return min(10.0, self.config.retry_backoff * (2 ** attempt) + random.uniform(0, 0.15))

    def request(self, path: str, payload: dict | None = None, stream: bool = False):
        data = json.dumps(payload).encode() if payload is not None else None
        attempts = max(0, self.config.retries) + 1
        for attempt in range(attempts):
            try:
                return self._attempt(path, data, stream)
            except APIError as exc:
                if attempt == attempts - 1 or not getattr(exc, "retryable", False):
                    raise
                delay = getattr(exc, "retry_after", None)
                time.sleep(delay if delay is not None else self._backoff(attempt))

    def _attempt(self, path: str, data: bytes | None, stream: bool):
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}
        key = os.environ.get(self.config.api_key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        # Do not forward authorization to redirects (including a redirect to another host).
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            response = urllib.request.build_opener(NoRedirect).open(req, timeout=self.config.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode(errors="replace")
            if key:
                detail = detail.replace(key, "[redacted]")
            error = APIError(f"API HTTP {exc.code}: {detail}")
            if exc.code in self.RETRYABLE:
                error.retryable = True  # type: ignore[attr-defined]
                if exc.headers:
                    retry_after = exc.headers.get("Retry-After")
                    if retry_after:
                        try:
                            error.retry_after = min(30.0, max(0.0, float(retry_after)))  # type: ignore[attr-defined]
                        except ValueError:
                            pass
            raise error from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = APIError(f"API connection failed: {exc}")
            error.retryable = True  # type: ignore[attr-defined]
            raise error from None
        if stream:
            return response
        with response:
            try:
                return json.load(response)
            except (ValueError, UnicodeError):
                raise APIError("API did not return valid JSON") from None

    def models(self) -> list[Model]:
        data = self.request("/models")
        records = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        if not isinstance(records, list):
            raise APIError("Model discovery expected a models/data list")
        return [metadata(record) for record in records if isinstance(record, dict) and record.get("id")]

    def discover(self) -> Model:
        try:
            models = self.models()
        except APIError:
            if self.config.model == "auto":
                raise
            models = []
        if self.config.model == "auto":
            # Prefer coding/chat families; filter obvious non-chat models. Selection is deterministic.
            candidates = [m for m in models if not any(word in m.id.lower() for word in ("embed", "whisper", "tts", "dall-e", "image", "moderation", "rerank", "transcri"))]
            if not candidates:
                raise APIError("No text models discovered. Set a model explicitly with --model.")
            def score(m: Model):
                name = m.id.lower()
                return (sum(word in name for word in ("code", "chat", "instruct", "gpt", "claude")), m.context_window or 0, m.id)
            model = max(candidates, key=score)
        else:
            model = next((m for m in models if m.id == self.config.model), Model(self.config.model))
        if model.context_window is None:
            try:
                detail = self.request("/models/" + urllib.parse.quote(model.id, safe=""))
                if isinstance(detail, dict):
                    enriched = metadata(detail)
                    model.context_window = enriched.context_window
                    model.max_output_tokens = model.max_output_tokens or enriched.max_output_tokens
                    model.source = enriched.source
            except APIError:
                pass  # OpenAI and many compatible APIs do not expose context limits.
        if self.config.context_window:
            model.context_window = self.config.context_window
            model.source = "configured override"
        return model

    def complete(self, model: Model, messages: list[dict], tools: list[dict], on_text: Callable[[str], None] | None = None) -> dict:
        limit = min(self.config.max_output_tokens, model.max_output_tokens or self.config.max_output_tokens)
        if self.config.api_mode == "responses":
            return self._responses(model.id, messages, tools, limit, on_text)
        payload = {"model": model.id, "messages": messages, "max_completion_tokens": limit, "stream": bool(on_text)}
        if tools:
            payload["tools"] = tools
        if on_text:
            # Ask OpenAI-compatible servers to report token usage in the final stream chunk.
            payload["stream_options"] = {"include_usage": True}
        # max_tokens is required by some older compatible servers. Retry only this parameter error.
        try:
            result = self.request("/chat/completions", payload, bool(on_text))
        except APIError as exc:
            text = str(exc)
            if "400" in text and "max_completion_tokens" in text:
                payload["max_tokens"] = payload.pop("max_completion_tokens")
                result = self.request("/chat/completions", payload, bool(on_text))
            elif "400" in text and "stream_options" in text:
                payload.pop("stream_options", None)
                result = self.request("/chat/completions", payload, bool(on_text))
            else:
                raise
        if not on_text:
            try:
                message = result["choices"][0]["message"]
            except (KeyError, IndexError, TypeError):
                raise APIError("Completion response contains no assistant message") from None
            usage = result.get("usage") or {}
            self.usage.add(model.id, usage.get("prompt_tokens"), usage.get("completion_tokens"))
            return message
        text_parts: list[str] = []
        calls: dict[int, dict] = {}
        stream_usage: dict = {}
        with result:
            for event in sse(result):
                if event.get("error"):
                    raise APIError(f"Stream error: {event['error']}")
                if event.get("usage"):
                    stream_usage = event["usage"]
                for choice in event.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        text_parts.append(delta["content"])
                        on_text(delta["content"])
                    for call in delta.get("tool_calls", []):
                        entry = calls.setdefault(call["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        entry["id"] += call.get("id", "")
                        for key in ("name", "arguments"):
                            entry["function"][key] += call.get("function", {}).get(key, "")
        if stream_usage:
            self.usage.add(model.id, stream_usage.get("prompt_tokens"), stream_usage.get("completion_tokens"))
        if not text_parts and not calls:
            raise APIError("Stream returned no text or tool calls")
        message = {"role": "assistant", "content": "".join(text_parts) or None}
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return message

    def _responses(self, model: str, messages: list[dict], tools: list[dict], limit: int, on_text) -> dict:
        items = []
        instructions = []
        for message in messages:
            if message["role"] == "system":
                instructions.append(message["content"])
            elif message["role"] == "tool":
                items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
            elif isinstance(message.get("content"), list):
                # Multimodal user turn: text + image parts.
                parts = []
                for piece in message["content"]:
                    if piece.get("type") == "text":
                        parts.append({"type": "input_text", "text": str(piece.get("text", ""))})
                    elif piece.get("type") == "image_url":
                        url = str((piece.get("image_url") or {}).get("url", ""))
                        parts.append({"type": "input_image", "image_url": url})
                items.append({"role": message["role"], "content": parts or [{"type": "input_text", "text": ""}]})
            else:
                if message.get("content"):
                    items.append({"role": message["role"], "content": message["content"]})
                for call in message.get("tool_calls", []):
                    items.append({"type": "function_call", "call_id": call["id"], **call["function"]})
        payload = {"model": model, "instructions": "\n\n".join(instructions), "input": items, "max_output_tokens": limit, "store": False}
        if tools:
            payload["tools"] = [{"type": "function", **tool["function"]} for tool in tools]
        # Nonstreaming Responses is supported by more compatible gateways.
        result = self.request("/responses", payload)
        usage = result.get("usage") or {}
        self.usage.add(model, usage.get("input_tokens"), usage.get("output_tokens"))
        parts, calls = [], []
        for item in result.get("output", []):
            if item.get("type") == "function_call":
                calls.append({"id": item["call_id"], "type": "function", "function": {"name": item["name"], "arguments": item["arguments"]}})
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "refusal"}:
                    parts.append(content.get("text", content.get("refusal", "")))
        if not parts and not calls:
            raise APIError("Responses API returned no text or tool calls")
        message = {"role": "assistant", "content": "".join(parts) or None}
        if calls:
            message["tool_calls"] = calls
        if on_text and parts:
            on_text("".join(parts))
        return message


def sse(response):
    data = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                payload = "\n".join(data)
                if payload == "[DONE]":
                    return
                try:
                    yield json.loads(payload)
                except ValueError:
                    raise APIError("Invalid JSON in API event stream") from None
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data and "\n".join(data) != "[DONE]":
        try:
            yield json.loads("\n".join(data))
        except ValueError:
            raise APIError("Incomplete API event stream") from None
