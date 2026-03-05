"""Eval runner: discovers and executes eval sets under evals/usecase/."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import logfire

from luma.agent import (
    Agent,
    AgentResult,
    build_system_prompt,
    build_user_message,
    parse_agent_response,
)
from luma.agent.tools import GetDislikedEventsTool, GetLikedEventsTool, QueryEventsTool
from luma.config import DEFAULT_AGENT_MODEL
from luma.event_store import EventStore, MemoryProvider
from luma.preference_store import MemoryPreferenceProvider, PreferenceStore

from .models import QueryInput

EVALS_DIR = Path(__file__).parent
_USECASE_DIR = EVALS_DIR / "usecase"


def _list_eval_sets() -> list[str]:
    if not _USECASE_DIR.exists():
        return []
    return sorted(
        p.relative_to(_USECASE_DIR).with_suffix("").as_posix()
        for p in _USECASE_DIR.rglob("*.py")
        if p.name != "__init__.py" and not p.name.startswith("_")
    )


def _load_dataset(name: str):
    module_path = _USECASE_DIR / f"{name}.py"
    if not module_path.is_file():
        print(f"Error: eval set '{name}' not found at {module_path}", file=sys.stderr)
        sys.exit(1)

    qualified = f"evals.usecase.{name.replace('/', '.')}"
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
    system_prompt = build_system_prompt()

    def task(inp: QueryInput) -> AgentResult:
        store = EventStore(MemoryProvider(events=inp.events))
        preferences = PreferenceStore(MemoryPreferenceProvider())
        tools = [
            QueryEventsTool(store),
            GetLikedEventsTool(preferences),
            GetDislikedEventsTool(preferences),
        ]
        user_message = build_user_message(inp.prompt, inp.params)
        agent = Agent(
            system_prompt=system_prompt,
            tools=tools,
            expected_output=parse_agent_response,
        )
        return agent.query(user_message)

    return task


def _load_env_local() -> None:
    from dotenv import load_dotenv

    env_file = Path(__file__).resolve().parents[1] / ".env.local"
    load_dotenv(env_file, override=False)


# ---------------------------------------------------------------------------
# Baseline: JSON storage + synthetic EvaluationReport reconstruction
# ---------------------------------------------------------------------------

def _report_to_baseline_json(report, system_prompt: str) -> dict:
    """Serialize a report's per-case and aggregate metrics to a JSON-safe dict."""
    prompt_hash = hashlib.md5(system_prompt.encode()).hexdigest()[:8]

    cases: dict[str, dict] = {}
    for case in report.cases:
        vals: dict[str, object] = {}
        for key, result in (case.assertions or {}).items():
            vals[key] = result.value
        for key, result in (case.scores or {}).items():
            vals[key] = result.value
        cases[case.name] = vals

    avg = report.averages()
    averages: dict[str, object] = {}
    if avg is not None:
        for key, val in {**avg.scores, **avg.metrics}.items():
            averages[key] = val
        if avg.assertions is not None:
            averages["assertions_pass_rate"] = avg.assertions

    return {
        "prompt_hash": prompt_hash,
        "model": DEFAULT_AGENT_MODEL,
        "averages": averages,
        "cases": cases,
    }


def _baseline_json_to_report(data: dict):
    """Reconstruct a minimal EvaluationReport from saved JSON for use as a diff baseline."""
    from pydantic_evals.evaluators.evaluator import EvaluatorSpec
    from pydantic_evals.reporting import EvaluationReport, EvaluationResult, ReportCase

    dummy_spec = EvaluatorSpec(name="baseline", arguments={})
    cases = []
    for case_name, vals in data.get("cases", {}).items():
        assertions: dict = {}
        scores: dict = {}
        for key, val in vals.items():
            if isinstance(val, bool):
                assertions[key] = EvaluationResult(
                    name=key, value=val, reason=None, source=dummy_spec
                )
            elif isinstance(val, (int, float)):
                scores[key] = EvaluationResult(
                    name=key, value=val, reason=None, source=dummy_spec
                )
        cases.append(
            ReportCase(
                name=case_name,
                inputs=None,
                metadata=None,
                expected_output=None,
                output=None,
                metrics={},
                attributes={},
                scores=scores,
                labels={},
                assertions=assertions,
                task_duration=0.0,
                total_duration=0.0,
            )
        )
    return EvaluationReport(name=data.get("model", "baseline"), cases=cases)


def _save_baseline(dataset_name: str, report, system_prompt: str) -> None:
    baseline_path = _USECASE_DIR / f"{dataset_name}.baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    data = _report_to_baseline_json(report, system_prompt)
    baseline_path.write_text(json.dumps(data, indent=2, default=str))
    print(f"Baseline saved to {baseline_path}")


def _load_baseline(dataset_name: str):
    """Load baseline JSON and return a synthetic EvaluationReport, or None."""
    baseline_path = _USECASE_DIR / f"{dataset_name}.baseline.json"
    if not baseline_path.exists():
        return None
    data = json.loads(baseline_path.read_text())
    return _baseline_json_to_report(data)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _parse_tag(raw: str) -> tuple[str, str]:
    """Parse 'key:value' into (key, value). Value 'true'/'false' kept as strings here."""
    if ":" not in raw:
        raise argparse.ArgumentTypeError(f"Tag must be key:value, got '{raw}'")
    key, value = raw.split(":", 1)
    return key, value


def _filter_by_tags(dataset, tags: list[tuple[str, str]]) -> bool:
    """Keep only cases whose metadata matches all given tags. Returns False if none match."""

    def _matches(case) -> bool:
        meta = case.metadata or {}
        for key, value in tags:
            actual = meta.get(key)
            if actual is None:
                return False
            if isinstance(actual, bool):
                if value.lower() not in ("true", "false"):
                    return False
                if actual != (value.lower() == "true"):
                    return False
            elif str(actual) != value:
                return False
        return True

    filtered = [c for c in dataset.cases if _matches(c)]
    if not filtered:
        return False
    dataset.cases = filtered
    return True


def _run_eval(
    eval_set: str,
    verbose: bool,
    save_baseline: bool,
    tags: list[tuple[str, str]] | None = None,
) -> None:
    system_prompt = build_system_prompt()
    prompt_hash = hashlib.md5(system_prompt.encode()).hexdigest()[:8]

    dataset = _load_dataset(eval_set)

    if tags:
        if not _filter_by_tags(dataset, tags):
            tag_str = ", ".join(f"{k}:{v}" for k, v in tags)
            print(f"  (no cases matching [{tag_str}] in {eval_set}, skipping)")
            return

    task = _make_task()
    report = dataset.evaluate_sync(
        task,
        metadata={"prompt_hash": prompt_hash, "model": DEFAULT_AGENT_MODEL},
    )

    if save_baseline:
        report.print(include_reasons=True)
        _save_baseline(eval_set, report, system_prompt)
    else:
        baseline_report = _load_baseline(eval_set)
        if baseline_report is not None:
            report.print(baseline=baseline_report, include_reasons=verbose)
        else:
            report.print(include_reasons=verbose)


def main() -> int:
    _load_env_local()
    logfire.configure(send_to_logfire=False, console=False)

    parser = argparse.ArgumentParser(description="Run agent eval sets")
    parser.add_argument("--set", dest="eval_set", help="Eval set name to run")
    parser.add_argument("--list", action="store_true", help="List available eval sets")
    parser.add_argument("--verbose", action="store_true", help="Show detailed output")
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="Save current results as baseline",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all discovered eval sets sequentially",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Shorthand for --tag smoke:true",
    )
    parser.add_argument(
        "--tag",
        action="append",
        dest="tags",
        metavar="KEY:VALUE",
        help="Filter cases by metadata tag (repeatable, e.g. --tag nature:edge_case)",
    )
    args = parser.parse_args()

    tags: list[tuple[str, str]] = []
    if args.smoke:
        tags.append(("smoke", "true"))
    for raw in args.tags or []:
        tags.append(_parse_tag(raw))

    if args.list:
        sets = _list_eval_sets()
        if not sets:
            print("No eval sets found.")
        else:
            print("Available eval sets:")
            for name in sets:
                print(f"  {name}")
            print(f"\nExample: make eval SET={sets[0]}")
            print(f"         make save-baseline SET={sets[0]}")
        return 0

    if args.all:
        sets = _list_eval_sets()
        if not sets:
            print("No eval sets found.")
            return 0
        for eval_set in sets:
            print(f"\n=== Running: {eval_set} ===")
            _run_eval(eval_set, args.verbose, args.save_baseline, tags=tags or None)
        return 0

    eval_set = args.eval_set or "query_command/smoke"
    _run_eval(eval_set, args.verbose, args.save_baseline, tags=tags or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
