"""Deterministic Tier 1–3 YAML keyword rubric scorer (50% weight of composite match_score)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Pattern, Tuple

import yaml

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
    """Evaluates job descriptions against the Tier 1–3 keyword rubric in config/keywords.yaml."""

    def __init__(self, config_path: Path = DEFAULT_KEYWORDS_PATH) -> None:
        self.config_path = config_path
        self.tiers = self._load_and_compile(config_path)

    @staticmethod
    def _load_and_compile(config_path: Path) -> Dict[str, Dict[str, Any]]:
        if not config_path.exists():
            raise FileNotFoundError(f"Keywords configuration file not found: {config_path}")

        with config_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

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

        return compiled_tiers

    def score(self, description_text: str) -> Tuple[float, Dict[str, Any]]:
        """Score a plain-text job description from 0.0 to 100.0 and return the group breakdown.

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
        breakdown: Dict[str, Any] = {
            "tier_scores": tier_scores,
            "matched_groups": matched_groups,
            "missing_groups": missing_groups,
        }
        return final_score, breakdown
