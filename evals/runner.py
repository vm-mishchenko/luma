"""Eval runner: discovers and executes eval sets."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from luma.agent import Agent, AgentResult
from luma.event_store import EventStore, MemoryProvider

from .models import QueryInput

EVALS_DIR = Path(__file__).parent
_INTERNAL_MODULES = {"__init__", "runner", "models", "evaluators"}


def _list_eval_sets() -> list[str]:
    return sorted(
        p.stem
        for p in EVALS_DIR.glob("*.py")
        if p.stem not in _INTERNAL_MODULES
    )


def _load_dataset(name: str):
    module_path = EVALS_DIR / f"{name}.py"
    if not module_path.is_file():
        print(f"Error: eval set '{name}' not found at {module_path}", file=sys.stderr)
        sys.exit(1)

    qualified = f"evals.{name}"
    spec = importlib.util.spec_from_file_location(qualified, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)

    if not hasattr(module, "dataset"):
        print(
            f"Error: eval set '{name}' has no 'dataset' attribute",
            file=sys.stderr,
        )
        sys.exit(1)

    return module.dataset


def _make_task():
    def task(inp: QueryInput) -> AgentResult:
        store = EventStore(MemoryProvider(events=inp.events))
        agent = Agent(store=store)
        return agent.query(inp.prompt, inp.params)

    return task


def _load_env_local() -> None:
    from dotenv import load_dotenv

    env_file = Path(__file__).resolve().parents[1] / ".env.local"
    load_dotenv(env_file, override=False)


def main() -> int:
    _load_env_local()
    parser = argparse.ArgumentParser(description="Run agent eval sets")
    parser.add_argument("--set", dest="eval_set", help="Eval set name to run")
    parser.add_argument("--list", action="store_true", help="List available eval sets")
    parser.add_argument("--verbose", action="store_true", help="Show detailed output")
    args = parser.parse_args()

    if args.list:
        sets = _list_eval_sets()
        if not sets:
            print("No eval sets found.")
        else:
            print("Available eval sets:")
            for name in sets:
                print(f"  {name}")
        return 0

    if args.eval_set is None:
        args.eval_set = "smoke"

    dataset = _load_dataset(args.eval_set)
    task = _make_task()
    report = dataset.evaluate_sync(task)
    report.print(include_reasons=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
