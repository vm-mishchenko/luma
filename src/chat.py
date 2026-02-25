"""Interactive chat REPL for luma chat command."""

from __future__ import annotations

import random
import sys
import threading
import time

from agent import Agent


class _Spinner:
    """Simple spinner that writes to stdout on a background thread."""

    _FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        idx = 0
        while not self._stop_event.is_set():
            frame = self._FRAMES[idx % len(self._FRAMES)]
            print(f"\r{frame} ", end="", flush=True)
            idx += 1
            time.sleep(0.08)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join()
        self._thread = None
        print("\r  \r", end="", flush=True)


def cmd_chat() -> int:
    print("luma chat (Ctrl+D to exit)")
    agent = Agent()
    history: list[dict[str, str]] = []

    while True:
        try:
            line = input("luma> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        message = line.strip()
        if not message:
            continue
        if message in ("/exit", "/quit"):
            return 0

        history.append({"role": "user", "content": line})
        spinner = _Spinner()
        saw_token = False
        try:
            spinner.start()
            for token in agent.run(history):
                if not saw_token:
                    spinner.stop()
                    saw_token = True
                print(f"{token} ", end="", flush=True)
                time.sleep(random.uniform(0.02, 0.06))
        except KeyboardInterrupt:
            pass
        finally:
            spinner.stop()

        print(file=sys.stdout)


