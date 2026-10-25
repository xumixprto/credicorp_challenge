from math import log2


def precision_at_k(ranked_ids: list[str], grades: dict[str, int], k: int) -> float:
    """Relevant means grade >= 2; unfilled positions count as irrelevant."""
    if k < 1:
        raise ValueError("k must be positive")
    return sum(grades.get(item, 0) >= 2 for item in ranked_ids[:k]) / k


def recall_at_k(ranked_ids: list[str], relevance: dict[str, int], k: int) -> float | None:
    """Only call with complete reference labels, never labels for retrieved results alone."""
    relevant = {item for item, grade in relevance.items() if grade >= 2}
    return len(set(ranked_ids[:k]) & relevant) / len(relevant) if relevant else None


def ndcg_at_k(
    ranked_ids: list[str], grades: dict[str, int], k: int, ideal_grades: list[int]
) -> float:
    def dcg(values: list[int]) -> float:
        return sum((2**grade - 1) / log2(index + 2) for index, grade in enumerate(values[:k]))

    ideal = dcg(sorted(ideal_grades, reverse=True))
    return dcg([grades.get(item, 0) for item in ranked_ids[:k]]) / ideal if ideal else 0.0
