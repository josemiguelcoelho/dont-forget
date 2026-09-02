from __future__ import annotations

import argparse
import threading
from datetime import datetime, timezone
from pathlib import Path

from .actions import LocalActions
from .agent import DontForgetAgent
from .checker import IntentionChecker
from .llm import DeterministicInterpreter
from .store import SQLiteStore


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Don't Forget MVP")
    parser.add_argument("--db", type=Path, default=Path(".dont-forget.db"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--check-every", type=float, default=5.0, metavar="SECONDS")
    args = parser.parse_args()

    store = SQLiteStore(args.db)
    actions = LocalActions([args.workspace])
    interpreter = DeterministicInterpreter()
    agent = DontForgetAgent(store, interpreter, actions, utc_now)
    checker = IntentionChecker(store, interpreter, actions, utc_now)
    stopped = threading.Event()

    def check_loop() -> None:
        while not stopped.wait(args.check_every):
            for notice in checker.run_due():
                print(f"\n{notice}", flush=True)

    thread = threading.Thread(target=check_loop, daemon=True)
    thread.start()

    try:
        while True:
            message = input("> ").strip()
            if not message:
                continue
            try:
                print(agent.receive(message))
            except (ValueError, OSError, PermissionError) as error:
                print(str(error))
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        stopped.set()
        thread.join(timeout=1)
        store.close()


if __name__ == "__main__":
    main()
