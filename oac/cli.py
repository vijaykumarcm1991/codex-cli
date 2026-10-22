from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .agent import Agent, Session
from .api import APIError, Client
from .config import Config, config_path, state_path
from .tools import Tools


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oac", description="OpenAI-compatible terminal coding agent")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Configure an API; keys are read from an environment variable")
    init.add_argument("--base-url", default="https://api.openai.com/v1")
    init.add_argument("--api-key-env", default="OPENAI_API_KEY")
    init.add_argument("--model", default="auto")
    init.add_argument("--api-mode", choices=["chat", "responses"], default="chat")
    init.add_argument("--context-window", type=int)
    sub.add_parser("config", help="Show configuration (no secret values)")
    sub.add_parser("sessions", help="List saved sessions")
    for name in ("models", "doctor", "run", "chat"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--base-url")
        cmd.add_argument("--model")
        cmd.add_argument("--api-mode", choices=["chat", "responses"])
        cmd.add_argument("--context-window", type=int)
        if name == "models":
            cmd.add_argument("--json", action="store_true")
        if name in {"run", "chat"}:
            cmd.add_argument("prompt", nargs="*", help="Prompt; run also accepts stdin")
            cmd.add_argument("--cwd", type=Path, default=Path.cwd())
            cmd.add_argument("--approval", choices=["ask", "read-only", "auto"], default="ask", help="auto grants unrestricted shell execution; this is not an OS sandbox")
            cmd.add_argument("--resume", metavar="SESSION_ID")
            cmd.add_argument("--max-steps", type=int)
            cmd.add_argument("--max-output-tokens", type=int)
            cmd.add_argument("--no-stream", action="store_true")
            cmd.add_argument("--json", action="store_true", help="Emit final answer and session ID as JSON (no token streaming)")
    return p


def status(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def approve(description: str) -> bool:
    # Never consume piped prompts as approval, or accept EOF as consent.
    if not sys.stdin.isatty():
        status("Denied: interactive approval requires a terminal. Use --approval auto only for trusted work.")
        return False
    status("\n" + description)
    try:
        print("Allow? [y/N] ", end="", file=sys.stderr, flush=True)
        return input().strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = Config.load()
        if args.command == "init":
            config = Config(base_url=args.base_url, api_key_env=args.api_key_env, model=args.model, api_mode=args.api_mode, context_window=args.context_window)
            Client(config)  # Validate the URL before persisting it.
            config.save()
            print(f"Saved {config_path()}. Export {config.api_key_env} if your API requires authentication.")
            return 0
        if args.command == "config":
            print(json.dumps(asdict(config), indent=2))
            return 0
        if args.command == "sessions":
            for path in sorted((state_path() / "sessions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                data = json.loads(path.read_text())
                print(f"{data['id']}  {data['updated']}  {data['model']}  {data['root']}")
            return 0
        for field in ("base_url", "model", "api_mode", "context_window", "max_steps", "max_output_tokens"):
            value = getattr(args, field, None)
            if value is not None:
                setattr(config, field, value)
        config.validate()
        client = Client(config)
        if args.command == "models":
            models = client.models()
            if args.json:
                print(json.dumps([asdict(m) for m in models], indent=2))
            else:
                for model in models:
                    print(f"{model.id}\tcontext={model.context_window or 'unknown'}\toutput={model.max_output_tokens or 'unknown'}")
            return 0
        model = client.discover()
        status(f"Model: {model.id}; context: {model.context_window or 'unknown (8192-token fallback)'}; source: {model.source}")
        if args.command == "doctor":
            result = client.complete(model, [{"role": "user", "content": "Reply with OK."}], [])
            if not result.get("content"):
                raise APIError("Doctor expected a text completion but the API returned no text")
            print(json.dumps({"model": asdict(model), "completion": result.get("content"), "api_mode": config.api_mode}, indent=2))
            return 0
        root = args.cwd.resolve()
        if not root.is_dir():
            raise ValueError("--cwd must be an existing directory")
        session = Session(root, args.resume)
        def emit(text: str) -> None:
            if not args.json:
                print(text, end="", flush=True)
        agent = Agent(client, model, Tools(root, args.approval, approve), session, emit, status, stream=not args.no_stream and not args.json)
        status(f"Session: {session.id}; project: {root}; approval: {args.approval}")
        initial = " ".join(args.prompt)
        if args.command == "run":
            prompt = initial or (sys.stdin.read() if not sys.stdin.isatty() else "")
            if not prompt.strip():
                raise ValueError("Supply a prompt or pipe one to oac run")
            answer = agent.run(prompt)
            if args.json:
                print(json.dumps({"session_id": session.id, "model": model.id, "answer": answer}))
            else:
                print()
            return 0
        status("Commands: /help /new /model ID /models /context /diff /compact /exit")
        while True:
            try:
                prompt = initial or input("\noac> ")
                initial = ""
            except EOFError:
                break
            if not prompt.strip():
                continue
            if prompt in {"/exit", "/quit"}:
                break
            if prompt == "/help":
                print("/new clears conversation; /compact keeps the latest turn; /model ID switches model; /models lists models; /context shows budget; /diff shows Git changes; /exit quits.")
            elif prompt == "/new":
                session = Session(root)
                agent.session = session
                status(f"Session: {session.id}")
            elif prompt.startswith("/model "):
                config.model = prompt.split(maxsplit=1)[1]
                model = client.discover()
                agent.model = model
                status(f"Model: {model.id}; context: {model.context_window or 'unknown'}")
            elif prompt == "/models":
                print("\n".join(m.id for m in client.models()))
            elif prompt == "/context":
                from .agent import estimate
                print(f"History estimate: {estimate(session.messages)} tokens; context: {model.context_window or 'unknown (8192 fallback)'}; output budget: {config.max_output_tokens}. Estimates use UTF-8 bytes, not a model tokenizer.")
            elif prompt == "/compact":
                indexes = [i for i, m in enumerate(session.messages) if m["role"] == "user"]
                if indexes:
                    session.messages = session.messages[indexes[-1]:]
                    session.save(model.id)
                print("Kept the latest turn; discarded earlier turns from this session.")
            elif prompt == "/diff":
                subprocess.run(["git", "--no-pager", "diff", "--no-ext-diff"], cwd=root, check=False)
            elif prompt.startswith("/"):
                print("Unknown command. Use /help.")
            else:
                try:
                    answer = agent.run(prompt)
                    if args.json:
                        print(json.dumps({"session_id": session.id, "model": model.id, "answer": answer}))
                    else:
                        print()
                except APIError as exc:
                    status(str(exc))
        return 0
    except (APIError, ValueError, OSError, KeyError) as exc:
        status(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        status("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
