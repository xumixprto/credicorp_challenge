"""Normalize rubric grades: failed=0, partial=1, successful=2; unknown stays blank."""

from typing import Literal


def behavior_score(grade: Literal[0, 1, 2] | None) -> float | None:
    return grade / 2 if grade is not None else None
