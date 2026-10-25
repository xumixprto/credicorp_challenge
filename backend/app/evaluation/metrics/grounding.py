def grounding_scores(verdicts: list[str]) -> dict[str, float | int | None]:
    """Evidence-relative claim rates, not an external truth or financial-advice assessment."""
    total = len(verdicts)
    supported = verdicts.count("supported")
    return {
        "claim_count": total,
        "groundedness": supported / total if total else None,
        "hallucination": (total - supported) / total if total else None,
    }
