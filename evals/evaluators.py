"""Custom evaluators for agent query evals."""

from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from luma.agent import EventListResult, QueryParamsResult, TextResult


@dataclass
class ResultTypeMatch(Evaluator):
    """Checks that actual result type matches expected."""

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        if ctx.expected_output is None:
            return True
        return type(ctx.output) is type(ctx.expected_output)


@dataclass
class ParamsMatch(Evaluator):
    """Compares QueryParamsResult params fields, returns field match ratio and exact match."""

    def evaluate(self, ctx: EvaluatorContext) -> dict[str, bool | float]:
        if not isinstance(ctx.output, QueryParamsResult):
            return {"field_match_ratio": 0.0, "exact_match": False}
        if not isinstance(ctx.expected_output, QueryParamsResult):
            return {"field_match_ratio": 0.0, "exact_match": False}

        actual = ctx.output.params.model_dump(exclude_none=True)
        expected = ctx.expected_output.params.model_dump(exclude_none=True)
        actual.pop("sort", None)
        expected.pop("sort", None)

        if not expected:
            return {"field_match_ratio": 1.0, "exact_match": True}

        all_keys = set(actual) | set(expected)
        matches = sum(1 for k in all_keys if actual.get(k) == expected.get(k))
        ratio = matches / len(all_keys) if all_keys else 1.0
        exact = actual == expected

        return {"field_match_ratio": ratio, "exact_match": exact}


@dataclass
class CoordinatesSet(Evaluator):
    """Checks that search_lat and search_lon are both set in QueryParamsResult."""

    def evaluate(self, ctx: EvaluatorContext) -> dict[str, bool]:
        if not isinstance(ctx.output, QueryParamsResult):
            return {"coordinates_set": False}
        params = ctx.output.params
        return {"coordinates_set": params.search_lat is not None and params.search_lon is not None}


@dataclass
class EventIDsMatch(Evaluator):
    """Computes precision and recall for EventListResult IDs."""

    def evaluate(self, ctx: EvaluatorContext) -> dict[str, float]:
        if not isinstance(ctx.output, EventListResult):
            return {"precision": 0.0, "recall": 0.0}
        if not isinstance(ctx.expected_output, EventListResult):
            return {"precision": 0.0, "recall": 0.0}

        predicted = set(ctx.output.ids)
        expected = set(ctx.expected_output.ids)

        if not predicted and not expected:
            return {"precision": 1.0, "recall": 1.0}

        correct = predicted & expected
        precision = len(correct) / len(predicted) if predicted else 0.0
        recall = len(correct) / len(expected) if expected else 0.0

        return {"precision": precision, "recall": recall}


@dataclass
class NDCGAtK(Evaluator):
    """NDCG@K with binary relevance for EventListResult."""

    k: int = 10

    def evaluate(self, ctx: EvaluatorContext) -> dict[str, float]:
        if not isinstance(ctx.output, EventListResult):
            return {f"ndcg_at_{self.k}": 0.0}
        if not isinstance(ctx.expected_output, EventListResult):
            return {f"ndcg_at_{self.k}": 0.0}

        predicted = ctx.output.ids
        expected_set = set(ctx.expected_output.ids)

        top_k = predicted[: self.k]
        dcg = sum(
            (1.0 if pid in expected_set else 0.0) / math.log2(i + 2)
            for i, pid in enumerate(top_k)
        )

        ideal_hits = min(len(expected_set), self.k)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))

        ndcg = dcg / idcg if idcg > 0 else 1.0
        return {f"ndcg_at_{self.k}": ndcg}


@dataclass
class TextNotEmpty(Evaluator):
    """Checks that TextResult contains non-empty text."""

    def evaluate(self, ctx: EvaluatorContext) -> dict[str, bool]:
        if not isinstance(ctx.output, TextResult):
            return {"text_not_empty": False}
        return {"text_not_empty": bool(ctx.output.text.strip())}


@dataclass
class Efficiency(Evaluator):
    """Reads agent.llm_call spans to measure token usage and call counts."""

    token_budget: int = 5000
    time_budget: float = 15.0

    def evaluate(self, ctx: EvaluatorContext) -> dict[str, bool | float | int]:
        try:
            span_tree = ctx.span_tree
        except Exception:
            return {
                "input_tokens": 0,
                "output_tokens": 0,
                "llm_calls": 0,
                "tool_calls": 0,
                "within_token_budget": True,
                "within_time_budget": True,
            }

        llm_spans = span_tree.find(lambda s: s.name == "agent.llm_call")
        tool_spans = span_tree.find(lambda s: s.name == "agent.tool_call")

        input_tokens = sum(
            int(s.attributes.get("input_tokens", 0)) for s in llm_spans
        )
        output_tokens = sum(
            int(s.attributes.get("output_tokens", 0)) for s in llm_spans
        )

        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "llm_calls": len(llm_spans),
            "tool_calls": len(tool_spans),
            "within_token_budget": input_tokens <= self.token_budget,
            "within_time_budget": ctx.duration <= self.time_budget,
        }


@dataclass
class NoUnnecessaryToolUse(Evaluator):
    """For QueryParamsResult: checks the agent resolved params without calling any tools."""

    def evaluate(self, ctx: EvaluatorContext) -> dict[str, bool]:
        if not isinstance(ctx.output, QueryParamsResult):
            return {"no_tool_use": False}
        try:
            span_tree = ctx.span_tree
        except Exception:
            return {"no_tool_use": True}
        tool_spans = span_tree.find(lambda s: s.name == "agent.tool_call")
        return {"no_tool_use": len(tool_spans) == 0}
