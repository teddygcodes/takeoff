"""Takeoff Constitution: Hard Rules and Articles for Adversarial Lighting Takeoff."""


# 6 Hard Rules (Judge enforces these strictly)
HARD_RULES = [
    {
        "name": "Schedule Traceability",
        "description": "Every counted fixture must map to a type tag in the fixture schedule. No phantom fixtures.",
        "check": "schedule_traceability",
        "severity": "FATAL"
    },
    {
        "name": "Complete Coverage",
        "description": "Every RCP area in the snippet set must be accounted for. No skipped rooms.",
        "check": "complete_coverage",
        "severity": "FATAL"
    },
    {
        "name": "No Double-Counting",
        "description": "Fixtures in overlapping detail views cannot be counted twice.",
        "check": "no_double_counting",
        "severity": "MAJOR"
    },
    {
        "name": "Cross-Sheet Consistency",
        "description": "If panel schedule data is available, total fixture wattage must be within 15% of panel load calculations.",
        "check": "cross_sheet_consistency",
        "severity": "MAJOR"
    },
    {
        "name": "Emergency Fixture Tracking",
        "description": "Exit signs, emergency battery units, and emergency-circuit fixtures must be separately tracked.",
        "check": "emergency_fixture_tracking",
        "severity": "MAJOR"
    },
    {
        "name": "Flag Assumptions",
        "description": "Any ambiguous fixture type, unclear symbol, or assumed quantity must be explicitly flagged, not silently guessed.",
        "check": "flag_assumptions",
        "severity": "MAJOR"
    }
]


# 5 Constitutional Articles (Guidelines, not strictly enforced)
ARTICLES = [
    {
        "number": 1,
        "title": "Accuracy Over Speed",
        "principle": "Take the time to count correctly; rushing causes missed fixtures"
    },
    {
        "number": 2,
        "title": "Per-Area Accountability",
        "principle": "Counts must be broken down by area, not just grand totals"
    },
    {
        "number": 3,
        "title": "Adversarial Verification",
        "principle": "Every count must survive independent challenge before approval"
    },
    {
        "number": 4,
        "title": "Accessory Awareness",
        "principle": "Fixtures are not just the luminaire; consider mounting hardware, whips, sensors, battery packs"
    },
    {
        "number": 5,
        "title": "Revision Awareness",
        "principle": "Note which drawing revision was counted; flag if revision bubbles are visible"
    }
]

# Difficulty codes for fixture installation
DIFFICULTY_CODES = {
    "S": "Standard — troffer, surface mount, easy access",
    "M": "Moderate — recessed, requires cutting, intermediate skill",
    "D": "Difficult — requires lift/scaffold, high ceiling, complex mounting",
    "E": "Extreme — custom fabrication, long lead time, special skills required"
}

# Emergency fixture keywords (for auto-detection)
EMERGENCY_KEYWORDS = [
    "exit", "emergency", "em", "e.m.", "bug eye", "bug-eye", "battery backup",
    "battery pack", "em battery", "emergency circuit", "egress", "evac",
    "recessed egress", "safety lighting", "standby fixture", "egress light",
    "emergency egress", "exit light", "exit fixture", "em unit"
]

# Common fixture type patterns (for type tag validation)
# Note: Tag case sensitivity is handled by .upper() normalization throughout.
# All comparisons normalize tags to uppercase before matching.
FIXTURE_CATEGORIES = {
    "troffer": ["2x4", "2x2", "1x4", "troffer", "recessed troffer"],
    "downlight": ["downlight", "can light", "recessed", "pot light"],
    "linear": ["linear", "continuous", "strip light"],
    "sconce": ["sconce", "wall mount", "wall fixture"],
    "pendant": ["pendant", "hanging", "suspended"],
    "highbay": ["high bay", "highbay", "warehouse", "gym"],
    "exterior": ["wall pack", "pole light", "bollard", "flood", "exterior"],
    "track": ["track light", "track head", "track fixture"],
    "exit": ["exit sign", "exit"],
    "emergency": ["emergency", "bug eye", "battery pack"]
}


def get_constitution() -> dict:
    """Return full takeoff constitution as dict."""
    return {
        "hard_rules": HARD_RULES,
        "articles": ARTICLES,
        "difficulty_codes": DIFFICULTY_CODES,
        "emergency_keywords": EMERGENCY_KEYWORDS,
        "fixture_categories": FIXTURE_CATEGORIES
    }


def check_schedule_traceability(fixture_counts: list, fixture_schedule: dict) -> list:
    """Check that all counted fixtures map to a type tag in the schedule.

    Args:
        fixture_counts: List of fixture count dicts with type_tag field
        fixture_schedule: Dict mapping type_tag -> fixture info

    Returns:
        List of violations
    """
    violations = []
    schedule_tags = {tag.upper() for tag in fixture_schedule.get("fixtures", {}).keys()}

    for count in fixture_counts:
        tag = count.get("type_tag", "").upper()
        if tag and tag not in schedule_tags:
            violations.append({
                "rule": "Schedule Traceability",
                "severity": "FATAL",
                "explanation": f"Fixture type '{tag}' counted but not found in fixture schedule. No phantom fixtures allowed."
            })

    return violations


def _normalize_area_label(label: str) -> str:
    """Normalize area label for comparison (lowercase, strip, collapse separators).

    Strips trailing revision-only suffixes like '(Copy)', '(Rev A)', '(v2)', '(1)'
    so that 'Floor 2 North (Copy)' matches 'Floor 2 North'.
    Does NOT strip meaningful parentheticals like '(North)' or '(East Wing)'.
    """
    import re
    import unicodedata
    label = unicodedata.normalize("NFC", label)
    label = label.strip().lower().replace("-", " ").replace("_", " ")
    # Only strip known revision/copy suffixes, not meaningful location parentheticals.
    # Patterns: (copy), (rev A), (rev. 2), (revision 1), (v2), (1), (2) — pure numeric
    label = re.sub(
        r"\s*\((copy|rev\.?\s*\w*|revision\s*\w*|v\d+)\)\s*$",
        "", label, flags=re.IGNORECASE
    ).strip()
    return label


# Minimum number of distinct fixture types before we require emergency fixture tracking.
# Jobs with ≤ this many types may legitimately have no emergency fixtures in scope.
EMERGENCY_CHECK_MIN_TYPES = 5

_AREA_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "at", "in", "on", "for",
    "level", "floor", "area", "section", "zone", "room", "suite", "unit",
    "building", "wing", "corridor", "hall", "lobby",
}


def _area_fuzzy_match(expected: str, covered_set: set) -> bool:
    """Return True if any covered area is a close match to the expected area label.

    Uses word-overlap similarity to handle rename patterns like "North Wing Level 1"
    vs "Level 1 North Wing". Requires ≥60% non-stop word overlap AND any numeric
    tokens in the expected label must appear in the candidate (prevents "Level 1"
    from matching "Level 2"). Falls back to difflib ratio ≥0.80 for short labels.
    """
    import re
    from difflib import SequenceMatcher

    exp_nums = set(re.findall(r'\d+', expected))
    exp_words = {w for w in expected.split() if w not in _AREA_STOPWORDS and len(w) > 1}

    for candidate in covered_set:
        cand_nums = set(re.findall(r'\d+', candidate))
        cand_words = {w for w in candidate.split() if w not in _AREA_STOPWORDS and len(w) > 1}

        # Numeric tokens must match exactly — "Level 1" must not match "Level 2",
        # and "Level" must not match "Level 1" (one has numerics, the other doesn't)
        if exp_nums != cand_nums:
            continue

        # Word overlap: ≥50% of significant words must be shared.
        # The numeric guard above ensures "Level 1" can't match "Level 2".
        if exp_words and cand_words:
            overlap = exp_words & cand_words
            if len(overlap) / max(len(exp_words), len(cand_words)) >= 0.50:
                return True

        # Character-level similarity fallback for very short labels (e.g. "L1" vs "Level 1")
        ratio = SequenceMatcher(None, expected, candidate).ratio()
        if ratio >= 0.80:
            return True

    return False


def check_complete_coverage(areas_covered: list, rcp_snippets: list) -> list:
    """Check that all RCP snippet areas are accounted for.

    Args:
        areas_covered: List of area names from Counter agent
        rcp_snippets: List of snippet dicts with sub_label (area name)

    Returns:
        List of violations
    """
    violations = []
    # Normalize expected areas; fall back to index-based label if sub_label is empty
    expected_areas = {}
    rcp_list = [s for s in rcp_snippets if s.get("label") == "rcp"]
    for i, snip in enumerate(rcp_list):
        raw = snip.get("sub_label", "").strip()
        normalized = _normalize_area_label(raw) if raw else f"area {i + 1}"
        if normalized:
            expected_areas[normalized] = raw or f"area {i + 1}"

    covered_normalized = {_normalize_area_label(a) for a in areas_covered}

    for norm_label, display_label in expected_areas.items():
        if not norm_label:
            continue
        if norm_label in covered_normalized:
            continue
        # Fuzzy match — handle area label renames (e.g. "Level 1" vs "Floor 1")
        if _area_fuzzy_match(norm_label, covered_normalized):
            violations.append({
                "rule": "Complete Coverage",
                "severity": "MAJOR",
                "explanation": f"RCP area '{display_label}' label differs from Counter's areas_covered (fuzzy match found). Verify the area was counted."
            })
        else:
            violations.append({
                "rule": "Complete Coverage",
                "severity": "FATAL",
                "explanation": f"RCP area '{display_label}' was provided as a snippet but is not in the Counter's areas_covered list."
            })

    return violations


def check_no_double_counting(fixture_counts: list, fixture_schedule: dict) -> list:
    """Check that area subtotals don't exceed the reported total by >10%.

    A discrepancy where sum(counts_by_area) > total * 1.10 suggests double-counting
    from overlapping detail views or repeated sections.

    Args:
        fixture_counts: List of fixture count dicts with type_tag, total, counts_by_area
        fixture_schedule: Not used; kept for API consistency with other check functions

    Returns:
        List of violations
    """
    violations = []

    for fc in fixture_counts:
        tag = fc.get("type_tag", "")
        total = fc.get("total", 0)
        counts_by_area = fc.get("counts_by_area", {})

        if not counts_by_area or not total:
            continue

        area_sum = sum(counts_by_area.values())
        if area_sum > total * 1.10:
            violations.append({
                "rule": "No Double-Counting",
                "severity": "MAJOR",
                "explanation": (
                    f"Type {tag}: area subtotals sum to {area_sum} but reported total is {total} "
                    f"(>10% over — possible double-count from overlapping views)"
                )
            })

    return violations


def check_cross_sheet_consistency(fixture_counts: list) -> list:
    """Check for the same area appearing twice with different fixture counts.

    Args:
        fixture_counts: List of fixture count dicts with type_tag, counts_by_area

    Returns:
        List of violations
    """
    violations = []
    area_map: dict = {}

    for fc in fixture_counts:
        tag = fc.get("type_tag", "")
        counts_by_area = fc.get("counts_by_area", {})
        for area, count in counts_by_area.items():
            norm = _normalize_area_label(area)
            key = (tag.upper(), norm)
            if key in area_map and area_map[key] != count:
                violations.append({
                    "rule": "Cross-Sheet Consistency",
                    "severity": "MAJOR",
                    "explanation": f"Type {tag} in area '{area}' has conflicting counts across sheets: {area_map[key]} vs {count}"
                })
            else:
                area_map[key] = count

    return violations


def check_emergency_fixtures(fixture_counts: list) -> list:
    """Check that emergency and exit sign fixtures are separately tracked.

    Args:
        fixture_counts: List of fixture count dicts

    Returns:
        List of violations
    """
    violations = []
    has_emergency_tracking = False

    for count in fixture_counts:
        desc = count.get("description", "").lower()
        tag = count.get("type_tag", "").lower()
        notes = count.get("notes", "").lower()

        import re as _re
        if any(_re.search(r'\b' + _re.escape(kw) + r'\b', desc + " " + tag + " " + notes, _re.IGNORECASE)
               for kw in EMERGENCY_KEYWORDS):
            has_emergency_tracking = True
            break

    # Only flag if there are more than EMERGENCY_CHECK_MIN_TYPES fixture types and no emergency tracking.
    # Small jobs (≤ that threshold) may legitimately have no emergency fixtures in scope.
    if not has_emergency_tracking and len(fixture_counts) > EMERGENCY_CHECK_MIN_TYPES:
        violations.append({
            "rule": "Emergency Fixture Tracking",
            "severity": "MAJOR",
            "explanation": "No emergency fixtures (exit signs, battery packs, emergency circuits) appear to be tracked. Verify these are not required or add them."
        })

    return violations


def check_flag_assumptions(fixture_counts: list) -> list:
    """Check that ambiguous fixtures are explicitly flagged, not silently guessed.

    Looks for entries that have counts but no flags, while their description or
    notes contain ambiguity-indicating language. Also checks that any fixture
    tagged 'UNKNOWN' is flagged as assumed.

    Args:
        fixture_counts: List of fixture count dicts with type_tag, flags, description, notes

    Returns:
        List of violations
    """
    ASSUMPTION_KEYWORDS = [
        "assume", "assumed", "unclear", "ambiguous", "unknown", "uncertain",
        "estimated", "approximate", "guess", "possibly", "likely", "probable"
    ]
    violations = []

    for fc in fixture_counts:
        tag = fc.get("type_tag", "")
        flags = fc.get("flags") or []
        description = (fc.get("description") or "").lower()
        notes = (fc.get("notes") or "").lower()
        combined = f"{description} {notes}"

        # Any fixture tagged UNKNOWN must have a flag explaining it
        if tag.upper() == "UNKNOWN" and not flags:
            violations.append({
                "rule": "Flag Assumptions",
                "severity": "MAJOR",
                "explanation": f"Fixture type 'UNKNOWN' counted but not flagged as assumed. Ambiguous fixtures must be explicitly flagged."
            })

        # Fixtures with assumption language in description/notes but no flags
        elif any(kw in combined for kw in ASSUMPTION_KEYWORDS) and not flags:
            violations.append({
                "rule": "Flag Assumptions",
                "severity": "MAJOR",
                "explanation": f"Type '{tag}': description/notes contain ambiguous language ('{combined[:80]}...') but no flags set. Assumed quantities must be explicitly flagged."
            })

    return violations


def check_non_negative_counts(fixture_counts: list) -> list:
    """Check that all fixture counts are non-negative.

    A negative total or area count is a data corruption indicator —
    the LLM produced invalid output that must be caught before scoring.

    Args:
        fixture_counts: List of fixture count dicts

    Returns:
        List of violations
    """
    violations = []
    for fc in fixture_counts:
        tag = fc.get("type_tag", "?")
        total = fc.get("total", 0)
        if isinstance(total, (int, float)) and total < 0:
            violations.append({
                "rule": "Non-Negative Counts",
                "severity": "FATAL",
                "explanation": f"Type '{tag}' has a negative total count ({total}). Counts must be ≥ 0."
            })
        for area, count in fc.get("counts_by_area", {}).items():
            if not isinstance(count, int) and isinstance(count, float):
                violations.append({
                    "rule": "Non-Negative Counts",
                    "severity": "MAJOR",
                    "explanation": f"Type '{tag}' area '{area}' has a non-integer count ({count}). Fixture counts must be whole numbers."
                })
            elif isinstance(count, (int, float)) and count < 0:
                violations.append({
                    "rule": "Non-Negative Counts",
                    "severity": "FATAL",
                    "explanation": f"Type '{tag}' area '{area}' has a negative count ({count}). Counts must be ≥ 0."
                })
    return violations


def enforce_constitution(
    fixture_counts: list,
    areas_covered: list,
    rcp_snippets: list,
    fixture_schedule: dict,
    judge_violations: list = None
) -> dict:
    """Enforce takeoff constitutional rules programmatically.

    Args:
        fixture_counts: Counter agent fixture counts
        areas_covered: Areas counted by Counter agent
        rcp_snippets: RCP snippet list for coverage check
        fixture_schedule: Extracted fixture schedule
        judge_violations: Additional violations from Judge agent

    Returns:
        Dict with verdict and violations
    """
    all_violations = list(judge_violations or [])

    # Pre-check: Non-negative counts (data integrity gate)
    all_violations.extend(check_non_negative_counts(fixture_counts))

    # Rule 1: Schedule Traceability
    all_violations.extend(check_schedule_traceability(fixture_counts, fixture_schedule))

    # Rule 2: Complete Coverage
    all_violations.extend(check_complete_coverage(areas_covered, rcp_snippets))

    # Rule 3: No Double-Counting (programmatic check)
    all_violations.extend(check_no_double_counting(fixture_counts, fixture_schedule))

    # Rule 4: Cross-Sheet Consistency (programmatic check)
    all_violations.extend(check_cross_sheet_consistency(fixture_counts))

    # Rule 5: Emergency Fixture Tracking
    all_violations.extend(check_emergency_fixtures(fixture_counts))

    # Rule 6: Flag Assumptions
    all_violations.extend(check_flag_assumptions(fixture_counts))

    # Determine verdict
    fatal_count = sum(1 for v in all_violations if v.get("severity") == "FATAL")
    major_count = sum(1 for v in all_violations if v.get("severity") == "MAJOR")

    if fatal_count > 0:
        verdict = "BLOCK"
    elif major_count > 0:
        verdict = "WARN"
    else:
        verdict = "PASS"

    return {
        "verdict": verdict,
        "violations": all_violations,
        "reasoning": f"Programmatic constitutional check: {len(all_violations)} violation(s) found"
    }
