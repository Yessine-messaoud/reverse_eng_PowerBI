from __future__ import annotations


def score_dashboard(dashboard_model: dict[str, object]) -> dict[str, object]:
    """Score dashboard quality using simple structural heuristics."""
    elements = dashboard_model.get("elements", [])
    count = len(elements) if isinstance(elements, list) else 0
    score = 0.45
    if count >= 1:
        score += 0.15
    if count >= 2:
        score += 0.15
    if count >= 3:
        score += 0.1
    if dashboard_model.get("title"):
        score += 0.05
    score = min(score, 0.95)

    confidence = "low"
    if score >= 0.8:
        confidence = "high"
    elif score >= 0.65:
        confidence = "medium"
    return {"score": round(score, 2), "confidence": confidence}
