"""
Reciprocal Rank Fusion (RRF) for merging multiple ranked retrieval lists.
"""
from __future__ import annotations


def rrf_fuse(
    ranked_lists: list[list[dict]],
    id_field: str = "name",
    k: int = 60,
    weights: list[float] | None = None,
) -> list[dict]:
    """
    Reciprocal Rank Fusion over multiple ranked result lists.
    Each item in a list must have `id_field` (e.g., "name") to identify it.
    Returns items sorted by RRF score descending, with added `rrf_score` field.
    Merges metadata by keeping highest-score occurrence.

    Optional `weights` list: one weight per ranked_list. Default all 1.0.
    Higher weight = items from that list rank higher in the fusion.
    Example: weights=[1.0, 1.0, 3.0] makes the third list 3x more important.
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)

    scores: dict[str, float] = {}
    best: dict[str, dict] = {}

    for i, ranked in enumerate(ranked_lists):
        w = weights[i] if i < len(weights) else 1.0
        for rank, item in enumerate(ranked, start=1):
            key = item.get(id_field, "")
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + w * (1.0 / (k + rank))
            if key not in best or item.get("score", 0.0) > best[key].get("score", 0.0):
                best[key] = item

    sorted_keys = sorted(scores, key=lambda x: scores[x], reverse=True)
    result = []
    for key in sorted_keys:
        item = dict(best[key])
        item["rrf_score"] = scores[key]
        result.append(item)
    return result


def deduplicate_files(file_list: list[dict], id_field: str = "path") -> list[dict]:
    """Remove duplicate files, keeping the one with highest score."""
    seen: dict[str, dict] = {}
    for item in file_list:
        key = item.get(id_field, "")
        if key and (key not in seen or item.get("score", 0.0) > seen[key].get("score", 0.0)):
            seen[key] = item
    return list(seen.values())
