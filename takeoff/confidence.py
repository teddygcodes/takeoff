"""Feature-based confidence scoring for lighting takeoffs (not vibes)."""

import json
import logging
from typing import List, Dict, Optional

from takeoff.constitution import _normalize_area_label, _area_fuzzy_match

logger = logging.getLogger(__name__)


# Feature weights — all positive, sum = 1.00
FEATURE_WEIGHTS = {
    "schedule_match_rate": 0.25,        # % of counted fixtures that trace to schedule
    "area_coverage": 0.20,              # % of visible RCP areas accounted for
    "adversarial_resolved": 0.15,       # % of Checker attacks resolved (conceded or defended)
    "constitutional_clean": 0.15,       # No violations = boost
    "cross_reference_match": 0.10,      # Panel schedule alignment
    "note_compliance": 0.10,            # Plan notes addressed
    "reconciler_coverage": 0.05         # Reconciler ran (strict/liability mode) = full credit; fast mode = 0
}

# Validate at module load time to catch weight editing mistakes immediately
assert abs(sum(FEATURE_WEIGHTS.values()) - 1.0) < 1e-9, (
    f"FEATURE_WEIGHTS must sum to 1.0, got {sum(FEATURE_WEIGHTS.values())}"
)


def calculate_confidence(
    fixture_counts: List[Dict],
    areas_covered: List[str],
    rcp_snippets: List[Dict],
    fixture_schedule: Dict,
    checker_attacks: List[Dict],
    reconciler_responses: List[Dict],
    constitutional_violations: List[Dict],
    mode: str,
    has_panel_schedule: bool = False,
    has_plan_notes: bool = False,
    notes_addressed: bool = False,
    total_corrected: bool = False
) -> Dict:
    """Calculate feature-based confidence score for a lighting takeoff.

    Args:
        fixture_counts: Final fixture count list from Counter/Reconciler
        areas_covered: Areas the Counter agent covered
        rcp_snippets: All RCP snippets provided
        fixture_schedule: Extracted fixture schedule dict
        checker_attacks: Attacks from Checker agent
        reconciler_responses: Responses from Reconciler agent
        constitutional_violations: Violations from Judge
        mode: "fast" | "strict" | "liability"
        has_panel_schedule: Whether a panel schedule snippet was provided
        has_plan_notes: Whether plan notes snippet was provided
        notes_addressed: Whether plan notes were addressed in the takeoff

    Returns:
        Dict with confidence score and features
    """
    logger.debug(
        "[TAKEOFF CONFIDENCE] Inputs: counts=%d areas=%d rcp=%d attacks=%d responses=%d violations=%d mode=%s",
        len(fixture_counts), len(areas_covered),
        len([s for s in rcp_snippets if s.get("label") == "rcp"]),
        len(checker_attacks), len(reconciler_responses), len(constitutional_violations), mode
    )

    features = {}

    # Feature 1: Schedule match rate
    # How many counted fixture types have a corresponding schedule entry
    schedule_tags = {tag.upper() for tag in fixture_schedule.get("fixtures", {}).keys()}
    if fixture_counts and schedule_tags:
        matched = sum(1 for fc in fixture_counts if fc.get("type_tag", "").upper() in schedule_tags)
        features["schedule_match_rate"] = matched / len(fixture_counts)
    elif not fixture_counts:
        features["schedule_match_rate"] = 0.0
    else:
        # No schedule available to match against — neutral
        features["schedule_match_rate"] = 0.5

    # Feature 2: Area coverage
    # % of RCP snippets that have corresponding areas in the count.
    # Uses the same normalization + fuzzy matching as constitution Rule 2 so that
    # a job passing programmatic coverage check also scores well here.
    rcp_snippet_areas = [s.get("sub_label", "").strip() for s in rcp_snippets if s.get("label") == "rcp" and s.get("sub_label")]
    covered_normalized = {_normalize_area_label(a) for a in areas_covered}

    if rcp_snippet_areas:
        matched_count = 0
        for raw_label in rcp_snippet_areas:
            norm = _normalize_area_label(raw_label)
            if norm in covered_normalized or _area_fuzzy_match(norm, covered_normalized):
                matched_count += 1
        features["area_coverage"] = matched_count / len(rcp_snippet_areas)
    else:
        # No named RCP areas to verify against — neutral score (0.5), not perfect credit.
        # This is distinct from partial coverage: 0 labeled areas means the check is
        # unverifiable, not that coverage is good. Awarding 1.0 would inflate confidence
        # for jobs with no area labels at all.
        features["area_coverage"] = 0.5

    # Feature 3: Adversarial resolved
    # What % of Checker attacks were explicitly addressed by Reconciler
    if checker_attacks:
        if reconciler_responses:
            resolved_attack_ids = {r.get("attack_id") for r in reconciler_responses}
            checker_attack_ids = {a.get("attack_id") for a in checker_attacks}
            resolved_ratio = len(resolved_attack_ids & checker_attack_ids) / len(checker_attack_ids)
            features["adversarial_resolved"] = resolved_ratio
        else:
            # No reconciler — attacks unaddressed (fast mode)
            features["adversarial_resolved"] = 0.0
    else:
        features["adversarial_resolved"] = 1.0  # No attacks = clean

    # Feature 4: Constitutional clean
    fatal_violations = sum(1 for v in constitutional_violations if v.get("severity") == "FATAL")
    major_violations = sum(1 for v in constitutional_violations if v.get("severity") == "MAJOR")

    if fatal_violations > 0:
        features["constitutional_clean"] = 0.0
    elif major_violations > 0:
        features["constitutional_clean"] = 0.5
    else:
        features["constitutional_clean"] = 1.0

    # Feature 5: Cross-reference match
    # Panel schedule was provided and Checker didn't flag wattage discrepancy
    if has_panel_schedule:
        panel_attack_exists = any(
            "cross_reference" in a.get("category", "").lower() or "panel" in a.get("description", "").lower()
            for a in checker_attacks
        )
        features["cross_reference_match"] = 0.4 if panel_attack_exists else 1.0
    else:
        features["cross_reference_match"] = 0.5  # Neutral — no panel data available

    # Feature 6: Note compliance
    if has_plan_notes:
        features["note_compliance"] = 1.0 if notes_addressed else 0.3
    else:
        features["note_compliance"] = 0.5  # Neutral — no notes provided

    # Feature 7: Reconciler coverage — 1.0 when Reconciler ran (strict/liability), 0.0 for fast mode
    features["reconciler_coverage"] = 0.0 if mode == "fast" else 1.0

    # Calculate weighted confidence
    # Base starts at 0.20 so worst-case (all features 0.0) resolves to VERY_LOW,
    # not the misleading MODERATE that a 0.50 base produced.
    base_confidence = 0.20

    weighted_sum = sum(
        features.get(feature, 0.0) * weight
        for feature, weight in FEATURE_WEIGHTS.items()
    )

    confidence_score = base_confidence + weighted_sum

    if logger.isEnabledFor(logging.DEBUG):
        breakdown = "  ".join(
            f"{f}={features.get(f, 0.0):.3f}×{w:+.3f}={features.get(f, 0.0)*w:+.3f}"
            for f, w in FEATURE_WEIGHTS.items()
        )
        logger.debug("[TAKEOFF CONFIDENCE] base=%.3f weighted_sum=%.3f pre_override=%.3f | %s",
                     base_confidence, weighted_sum, confidence_score, breakdown)

    # Penalize if Counter's grand_total required auto-correction (math error)
    if total_corrected:
        confidence_score -= 0.05
        logger.debug("[TAKEOFF CONFIDENCE] auto-correct penalty -0.05 → %.3f", confidence_score)

    # Clamp to [0.0, 1.0]
    confidence_score = max(0.0, min(1.0, confidence_score))

    # HARD OVERRIDE based on violation severity — caps the score regardless of features.
    # Individual per-violation additive penalties were removed: they were always overwritten
    # by the caps below, making them dead code. The caps alone are the correct mechanism.
    fatal_count = sum(1 for v in constitutional_violations if v.get("severity") == "FATAL")
    major_count = sum(1 for v in constitutional_violations if v.get("severity") == "MAJOR")
    minor_count = sum(1 for v in constitutional_violations if v.get("severity") == "MINOR")

    if fatal_count > 0:
        confidence_score = 0.25
        logger.debug("[TAKEOFF CONFIDENCE] HARD OVERRIDE: %d FATAL → 0.25 (VERY_LOW)", fatal_count)
    elif major_count > 0:
        major_cap = max(0.20, 0.40 - (major_count - 1) * 0.05)
        confidence_score = min(confidence_score, major_cap)
        logger.debug("[TAKEOFF CONFIDENCE] HARD OVERRIDE: %d MAJOR → cap %.2f → %.3f", major_count, major_cap, confidence_score)
    elif minor_count > 0:
        minor_cap = max(0.35, 0.50 - (minor_count - 1) * 0.03)
        confidence_score = min(confidence_score, minor_cap)
        logger.debug("[TAKEOFF CONFIDENCE] HARD OVERRIDE: %d MINOR → cap %.2f → %.3f", minor_count, minor_cap, confidence_score)
    else:
        logger.debug("[TAKEOFF CONFIDENCE] PASS — final score %.3f", confidence_score)

    # Determine band
    if confidence_score >= 0.85:
        confidence_band = "HIGH"
    elif confidence_score >= 0.65:
        confidence_band = "MODERATE"
    elif confidence_score >= 0.40:
        confidence_band = "LOW"
    else:
        confidence_band = "VERY_LOW"

    logger.debug("[TAKEOFF CONFIDENCE] Final: %.3f (%s)", confidence_score, confidence_band)

    return {
        "score": round(confidence_score, 3),
        "band": confidence_band,
        "features": features,
        "features_json": json.dumps(features, indent=2)
    }


def format_confidence_explanation(confidence_result: Dict) -> str:
    """Format confidence explanation for user output.

    Args:
        confidence_result: Confidence calculation result

    Returns:
        Human-readable explanation string
    """
    features = confidence_result["features"]
    score = confidence_result["score"]
    band = confidence_result["band"]

    lines = [
        f"CONFIDENCE: {score:.2f} ({band})",
        ""
    ]

    # Schedule match rate
    schedule_match = features.get("schedule_match_rate", 0.0)
    lines.append(f"- Schedule traceability: {schedule_match * 100:.0f}% of fixture types verified")

    # Area coverage
    area_cov = features.get("area_coverage", 0.0)
    if area_cov >= 1.0:
        lines.append("- Area coverage: all RCP areas accounted for ✓")
    elif area_cov >= 0.8:
        lines.append(f"- Area coverage: {area_cov * 100:.0f}% (some areas may be missing)")
    else:
        lines.append(f"- Area coverage: {area_cov * 100:.0f}% (significant gaps)")

    # Adversarial
    adv_resolved = features.get("adversarial_resolved", 0.0)
    if adv_resolved >= 1.0:
        lines.append("- Checker attacks: all resolved ✓")
    elif adv_resolved == 0.0:
        lines.append("- Checker attacks: unaddressed (fast mode or no Reconciler)")
    else:
        lines.append(f"- Checker attacks: {adv_resolved * 100:.0f}% resolved")

    # Constitutional
    const_clean = features.get("constitutional_clean", 1.0)
    if const_clean >= 1.0:
        lines.append("- Constitutional violations: none ✓")
    elif const_clean >= 0.5:
        lines.append("- Constitutional violations: major (see flags)")
    else:
        lines.append("- Constitutional violations: fatal (takeoff blocked)")

    # Panel cross-reference
    xref = features.get("cross_reference_match", 0.5)
    if xref >= 1.0:
        lines.append("- Panel cross-reference: no discrepancies ✓")
    elif xref == 0.5:
        lines.append("- Panel cross-reference: no panel schedule provided")
    else:
        lines.append("- Panel cross-reference: wattage discrepancy flagged")

    # Reconciler coverage
    reconciler_cov = features.get("reconciler_coverage", 1.0)
    if reconciler_cov < 1.0:
        lines.append("- Fast mode: Reconciler review skipped (no credit for reconciler_coverage)")

    # Feature breakdown table
    lines.append("\nFeature Breakdown:")
    for feature, value in features.items():
        weight = FEATURE_WEIGHTS.get(feature, 0.0)
        contribution = value * weight
        lines.append(f"  {feature}: {value:.2f} × {weight:+.2f} = {contribution:+.3f}")

    return "\n".join(lines)
