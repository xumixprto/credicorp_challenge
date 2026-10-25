"""Per-invocation context, also visible to concurrent tool tasks via ContextVar."""

from contextvars import ContextVar

from shared.evaluations import RunRecord

active_run: ContextVar[RunRecord | None] = ContextVar("evaluation_run", default=None)
