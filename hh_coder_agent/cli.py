"""Command line interface for HH Coder Agent."""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from .agent import HHCoderAgent
from .models import OpenAIChatClient


def build_agent(args) -> HHCoderAgent:
    root = Path(args.cwd).resolve()
    client = OpenAIChatClient(root=root, model=args.model, base_url=args.base_url, api_key=args.api_key)
    agent = HHCoderAgent(
        model_client=client,
        root=root,
        approval=args.approval,
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
    )
    return agent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HH Coder Agent")
    parser.add_argument("prompt", nargs="*", help="Run one task and exit. Empty starts REPL.")
    parser.add_argument("--cwd", default=".", help="Workspace root")
    parser.add_argument("--approval", choices=["ask", "auto", "never"], default="ask")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=2048)
    args = parser.parse_args(argv)

    agent = build_agent(args)
    loaded = agent.cron.start()
    lock = threading.Lock()

    def run_turn(text: str) -> None:
        with lock:
            answer = agent.ask(text)
            print(f"\n{answer}\n")

    def queue_loop() -> None:
        while True:
            time.sleep(0.2)
            if not agent.cron.has_queue() or not lock.acquire(blocking=False):
                continue
            try:
                answer = agent.ask("[cron tick]")
                print(f"\n[cron]\n{answer}\n")
            finally:
                lock.release()

    threading.Thread(target=queue_loop, daemon=True).start()
    if args.prompt:
        run_turn(" ".join(args.prompt))
        return 0

    print(f"HH Coder Agent started at {agent.workspace.root}")
    print(f"restored {loaded} cron jobs. Commands: /exit, /session, /tasks, /crons")
    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return 0
        if not text:
            continue
        if text in {"/exit", "/quit"}:
            print("bye")
            return 0
        if text == "/session":
            print(agent.session_store.path(agent.session["id"]))
            continue
        if text == "/tasks":
            print(agent.tasks.format())
            continue
        if text == "/crons":
            print(agent.cron.list_text())
            continue
        run_turn(text)


if __name__ == "__main__":
    raise SystemExit(main())

