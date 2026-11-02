"""Hard knockout filters, workplace classification, HTML stripping, and SHA-256 deduplication hash."""

from __future__ import annotations

import hashlib
import html
import re
from typing import Literal, Optional, Tuple

from bs4 import BeautifulSoup

WorkplaceType = Literal["Remote", "Local", "Hybrid"]

# 1. Excluded seniority / management / intern prefixes & tokens (word-boundary matched)
EXCLUDED_TITLE_PATTERN = re.compile(
    r"\b(?:staff|principal|director|vp|vice\s+president|head\s+of|intern|internship|lead|manager)\b",
    re.IGNORECASE,
)

# 2. Target role families (Primary + Accepted Variations)
TARGET_TITLE_PATTERN = re.compile(
    r"(?:data\s+scien|ai\s+engineer|artificial\s+intelligence\s+engineer|"
    r"machine\s+learning|ml\s+engineer|applied\s+ai|data\s+engineer|"
    r"ml\s*ops|genai|generative\s+ai)",
    re.IGNORECASE,
)

# 3. Mode 2: Local <= 25 Min Commute (~15-mile radius around Raleigh / RTP, NC)
LOCAL_RTP_PATTERN = re.compile(
    r"\b(?:raleigh|cary|morrisville|rtp|research\s+triangle|garner|apex|durham|wake)\b",
    re.IGNORECASE,
)

# 4. Mode 1: Remote indicators & Non-US exclusions
REMOTE_INCLUSION_PATTERN = re.compile(
    r"\b(?:remote|anywhere|distributed|work\s+from\s+home|wfh)\b",
    re.IGNORECASE,
)

REMOTE_EXCLUSION_PATTERN = re.compile(
    r"\b(?:emea|apac|latam|uk|united\s+kingdom|canada|ontario|quebec|alberta|"
    r"british\s+columbia|europe|eu\b|india|london|berlin|paris|tokyo|singapore|"
    r"australia|brazil|mexico|dublin|amsterdam|toronto|vancouver|poland|spain|"
    r"germany|france|korea|japan)\b",
    re.IGNORECASE,
)

US_INDICATOR_PATTERN = re.compile(
    r"\b(?:us|usa|u\.s\.|u\.s\.a\.|united\s+states|north\s+america|americas)\b",
    re.IGNORECASE,
)

HYBRID_PATTERN = re.compile(r"\bhybrid\b", re.IGNORECASE)


def lowercase_alphanumeric(value: str) -> str:
    """Normalize a string to lowercase alphanumeric characters only."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def compute_job_hash(company: str, title: str, workplace_type: str) -> str:
    """Compute deterministic SHA-256 deduplication key per Stage 2 spec.

    Formula:
        SHA-256(lowercase_alphanumeric(company) | lowercase_alphanumeric(title) | lowercase(workplace_type))
    """
    norm_company = lowercase_alphanumeric(company)
    norm_title = lowercase_alphanumeric(title)
    norm_workplace = (workplace_type or "").strip().lower()
    composite = f"{norm_company}|{norm_title}|{norm_workplace}"
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()


def strip_html(raw_html: Optional[str]) -> str:
    """Convert HTML-encoded job descriptions into clean plain text."""
    if not raw_html:
        return ""
    # Unescape HTML entities first in case of double-encoded Greenhouse payloads
    unescaped = html.unescape(raw_html)
    soup = BeautifulSoup(unescaped, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse excessive blank lines and trailing spaces
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned


def is_excluded_title(title: str) -> bool:
    """Return True if the job title contains a knocked-out seniority or intern prefix."""
    if not title:
        return True
    return bool(EXCLUDED_TITLE_PATTERN.search(title))


def is_target_title(title: str) -> bool:
    """Return True if the job title matches our target Data Science / AI / ML / Data Engineering families."""
    if not title:
        return False
    return bool(TARGET_TITLE_PATTERN.search(title))


def classify_location(location_raw: Optional[str]) -> Optional[WorkplaceType]:
    """Evaluate location against Local (Raleigh/RTP <=25 min) and US-Remote rules.

    Returns:
        'Local', 'Hybrid', or 'Remote' if the location passes the filter;
        None if the location should be rejected.
    """
    if not location_raw or not location_raw.strip():
        return None

    loc = location_raw.strip()

    # Check Mode 2 first: Local Raleigh/RTP commute radius
    if LOCAL_RTP_PATTERN.search(loc):
        if HYBRID_PATTERN.search(loc):
            return "Hybrid"
        return "Local"

    # Check Mode 1: US-eligible Remote
    if REMOTE_INCLUSION_PATTERN.search(loc):
        # If explicitly tagged US alongside global hubs, allow if US is mentioned,
        # otherwise reject if non-US exclusion regions are present.
        has_non_us = bool(REMOTE_EXCLUSION_PATTERN.search(loc))
        has_us = bool(US_INDICATOR_PATTERN.search(loc))
        if has_non_us and not has_us:
            return None
        return "Remote"

    return None


def evaluate_posting(
    company: str,
    title: str,
    location_raw: Optional[str],
) -> Tuple[bool, Optional[WorkplaceType], Optional[str], str]:
    """Run all Step 3.1 Hard Knockout Filters on a job posting.

    Returns:
        Tuple of (passed, workplace_type, job_hash, reason)
    """
    if is_excluded_title(title):
        return False, None, None, "Excluded seniority/prefix in title"

    if not is_target_title(title):
        return False, None, None, "Title outside target role families"

    workplace_type = classify_location(location_raw)
    if workplace_type is None:
        return False, None, None, f"Location rejected ({location_raw!r})"

    job_hash = compute_job_hash(company, title, workplace_type)
    return True, workplace_type, job_hash, "Passed"
