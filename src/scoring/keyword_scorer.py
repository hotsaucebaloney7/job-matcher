"""Deterministic Tier 1–3 YAML keyword rubric scorer (50% weight of composite match_score) and Experience Tier evaluator."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Pattern, Tuple

import yaml

from src.scoring.filters import (
    SENIOR_TITLE_PATTERN,
    classify_experience_tier,
    extract_min_years_experience,
)

DEFAULT_KEYWORDS_PATH = Path(__file__).resolve().parents[2] / "config" / "keywords.yaml"


def _compile_keyword_pattern(term: str) -> Pattern[str]:
    """Compile a boundary-aware case-insensitive regex for a keyword or phrase.

    Handles terms containing punctuation (e.g., 'm.s.', 'ph.d.', 'ci/cd', 'a/b testing')
    while preventing substring false positives (e.g., 'rag' in 'leverage' or 'git' in 'digital').
    """
    escaped = re.escape(term.strip())
    # Allow flexible whitespace in multi-word phrases
    escaped = escaped.replace(r"\ ", r"\s+")
    prefix = r"\b" if term and (term[0].isalnum() or term[0] == "_") else r"(?<!\w)"
    suffix = r"\b" if term and (term[-1].isalnum() or term[-1] == "_") else r"(?!\w)"
    return re.compile(f"{prefix}{escaped}{suffix}", re.IGNORECASE)


class KeywordScorer:
    """Evaluates job descriptions against the Tier 1–3 keyword rubric and experience rules in config/keywords.yaml."""

    def __init__(self, config_path: Path = DEFAULT_KEYWORDS_PATH) -> None:
        self.config_path = config_path
        self.tiers, self.experience_rules = self._load_and_compile(config_path)

    @staticmethod
    def _load_and_compile(config_path: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        if not config_path.exists():
            raise FileNotFoundError(f"Keywords configuration file not found: {config_path}")

        with config_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        exp_cfg = raw.get("experience_rules", {}) or {}
        experience_rules = {
            "primary_max_years": int(exp_cfg.get("primary_max_years", 3)),
            "stretch_max_years": int(exp_cfg.get("stretch_max_years", 5)),
            "hard_knockout_min_years": int(exp_cfg.get("hard_knockout_min_years", 6)),
            "stretch_penalty_points": float(exp_cfg.get("stretch_penalty_points", 5.0)),
        }

        tiers_cfg = raw.get("tiers", {}) or {}
        compiled_tiers: Dict[str, Dict[str, Any]] = {}

        for tier_key, tier_data in tiers_cfg.items():
            points_per_group = float(tier_data.get("points_per_group", 0))
            max_points = float(tier_data.get("max_points", 0))
            groups_raw = tier_data.get("groups", {}) or {}

            compiled_groups: Dict[str, List[Tuple[str, Pattern[str]]]] = {}
            for group_name, terms in groups_raw.items():
                compiled_groups[group_name] = [
                    (str(term), _compile_keyword_pattern(str(term))) for term in (terms or [])
                ]

            compiled_tiers[tier_key] = {
                "name": tier_data.get("name", tier_key),
                "points_per_group": points_per_group,
                "max_points": max_points,
                "groups": compiled_groups,
            }

        return compiled_tiers, experience_rules

    def evaluate_experience(self, description_text: str, title: str = "") -> Dict[str, Any]:
        """Extract minimum years required and classify into Primary (1-3 yrs), Stretch (3-5 yrs), or Knockout (6+ yrs)."""
        primary_max = self.experience_rules["primary_max_years"]
        stretch_max = self.experience_rules["stretch_max_years"]
        knockout_min = self.experience_rules["hard_knockout_min_years"]
        stretch_penalty = self.experience_rules["stretch_penalty_points"]

        min_years = extract_min_years_experience(description_text)
        tier_label = classify_experience_tier(
            min_years,
            title=title,
            primary_max=primary_max,
            stretch_max=stretch_max,
        )

        is_knockout = min_years is not None and min_years >= knockout_min
        is_stretch = False
        if min_years is not None and primary_max < min_years <= stretch_max:
            is_stretch = True
        elif min_years is None and SENIOR_TITLE_PATTERN.search(title or ""):
            is_stretch = True

        penalty = stretch_penalty if is_stretch else 0.0
        return {
            "min_years_required": min_years,
            "experience_tier": tier_label,
            "experience_penalty": penalty,
            "experience_knockout": is_knockout,
        }

    def score(self, description_text: str, title: str = "") -> Tuple[float, Dict[str, Any]]:
        """Score a plain-text job description from 0.0 to 100.0 and return the group & experience breakdown.

        Returns:
            Tuple of (keyword_score, matched_keywords_dict)
        """
        text = description_text or ""
        total_score = 0.0
        tier_scores: Dict[str, float] = {}
        matched_groups: Dict[str, List[str]] = {}
        missing_groups: List[str] = []

        for tier_key, tier_info in self.tiers.items():
            pts_per_group = tier_info["points_per_group"]
            max_pts = tier_info["max_points"]
            tier_earned = 0.0

            for group_name, term_patterns in tier_info["groups"].items():
                found_terms = [term for term, pattern in term_patterns if pattern.search(text)]
                if found_terms:
                    matched_groups[group_name] = found_terms
                    tier_earned += pts_per_group
                else:
                    missing_groups.append(group_name)

            tier_capped = min(tier_earned, max_pts)
            tier_scores[tier_key] = tier_capped
            total_score += tier_capped

        final_score = round(min(max(total_score, 0.0), 100.0), 2)
        exp_info = self.evaluate_experience(text, title=title)
        breakdown: Dict[str, Any] = {
            "tier_scores": tier_scores,
            "matched_groups": matched_groups,
            "missing_groups": missing_groups,
            **exp_info,
        }
        return final_score, breakdown
