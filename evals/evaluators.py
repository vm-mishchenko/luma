"""Custom evaluators for agent query evals."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from luma.agent import EventListResult, QueryParamsResult, TextResult


@dataclass
class ResultTypeMatch(Evaluator):
    """Checks that actual result type matches expected (TextResult vs EventListResult)."""

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        if ctx.expected_output is None:
            return True
        return type(ctx.output) is type(ctx.expected_output)


@dataclass
class NotEmpty(Evaluator):
    """Checks that the result contains meaningful content."""

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        if isinstance(ctx.output, TextResult):
            return bool(ctx.output.text.strip())
        if isinstance(ctx.output, EventListResult):
            return len(ctx.output.ids) > 0
        if isinstance(ctx.output, QueryParamsResult):
            return True
        return False
