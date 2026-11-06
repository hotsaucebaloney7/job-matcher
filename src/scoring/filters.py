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
    r"\b(?:staff|principal|director|vp|vice\s+president|head\s+of|chief|supervisory|intern|internship|lead|manager)\b",
    re.IGNORECASE,
)

# 2. Target role families (Primary + Accepted Variations + Federal Quantitative Series)
TARGET_TITLE_PATTERN = re.compile(
    r"(?:data\s+scien|ai\s+engineer|artificial\s+intelligence\s+engineer|"
    r"machine\s+learning|ml\s+engineer|applied\s+ai|data\s+engineer|"
    r"ml\s*ops|genai|generative\s+ai|operations\s+research\s+analyst|statistician)",
    re.IGNORECASE,
)

# 3. Mode 2: Local <= 25 Min Commute (~15-mile radius around Raleigh / RTP, NC)
LOCAL_RTP_PATTERN = re.compile(
    r"\b(?:raleigh|cary|morrisville|rtp|research\s+triangle|garner|apex|durham|wake)\b",
    re.IGNORECASE,
)

# 3b. USAJOBS-Specific Region: Washington, DC, Fort Meade, MD, and surrounding DMV / Baltimore-Washington corridor
USAJOBS_DC_FORT_MEADE_PATTERN = re.compile(
    r"(?:"
    r"\bwashington\s*,?\s*(?:dc|d\.c\.|district\s+of\s+columbia)\b|"
    r"\bdistrict\s+of\s+columbia\b|"
    r"\b(?:fort|ft\.?)\s+george\s+g\.?\s+meade\b|"
    r"\b(?:fort|ft\.?)\s+meade\b|"
    r"\b(?:annapolis\s+junction|odenton|laurel|columbia|linthicum|hanover|elkridge|"
    r"baltimore|silver\s+spring|bethesda|rockville|greenbelt|suitland|college\s+park|"
    r"new\s+carrollton|lanham|bowie|hyattsville|adele|adelphi|andrews\s+afb|joint\s+base\s+andrews|"
    r"fort\s+detrick|aberdeen\s+proving\s+ground|apg|patuxent\s+river|indian\s+head)\b|"
    r"\b(?:arlington|alexandria|mclean|tysons|reston|herndon|chantilly|fairfax|"
    r"falls\s+church|springfield|fort\s+belvoir|quantico|dahlgren|pentagon)\b"
    r")",
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
SENIOR_TITLE_PATTERN = re.compile(r"\b(?:senior|sr\.?)\b", re.IGNORECASE)

# 5. High-precision detector for "fake remote" postings whose description mandates
# out-of-state hybrid/in-office attendance or commutable residency (e.g. "3 days per week on-site in Frisco, TX").
# Avoids matching benign phrases like "hybrid cloud", "in-office perks", or "relocation (if needed)".
MANDATORY_ONSITE_HYBRID_PATTERN = re.compile(
    r"(?:"
    r"\bthis\s+is\s+a\s+hybrid\s+(?:role|position|opportunity)\b|"
    r"\bwe\s+use\s+a\s+hybrid\s+work\s+model\b|"
    r"\blocation[- ]based\s+hybrid\s+policy\b|"
    r"\b(?:\d+|one|two|three|four|five)\s+days?\s+(?:per|a)\s+week\s+(?:on[- ]site|in\s+(?:the\s+)?office)\b|"
    r"\bin\s+(?:the\s+)?office\s+(?:\d+|one|two|three|four|five)\s+days?\s+(?:per|a)\s+week\b|"
    r"\bmust\s+(?:reside|live)\s+within\s+(?:a\s+)?commutable\s+distance\b|"
    r"\bexpect\s+all\s+staff\s+to\s+be\s+in\s+one\s+of\s+our\b"
    r")",
    re.IGNORECASE,
)

# Experience extraction regexes:
# Matches phrases like "3+ years of experience", "3-5 years of experience",
# "minimum of 4 years", "at least 2 years of professional experience",
# "5+ years in machine learning / data science / software engineering"
EXPERIENCE_PATTERNS = [
    re.compile(
        r"\b(?:at\s+least|minimum\s+of|min\.?\s+of)?\s*(\d{1,2})\s*(?:\+|plus|\s*(?:\-|to|–)\s*\d{1,2})?\s*"
        r"(?:years?|yrs?)\b[^.\n]{0,65}?\b(?:experience|exp\b|background|track\s+record|"
        r"in\s+(?:data|machine\s+learning|ml|ai|software|engineering|analytics|applied|python|related))",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:experience|background)\s*(?:of|:|-)?\s*(\d{1,2})\s*(?:\+|plus|\s*(?:\-|to|–)\s*\d{1,2})?\s*(?:years?|yrs?)\b",
        re.IGNORECASE,
    ),
]

# Phrases to ignore when extracting candidate years of experience (e.g., company age or legal age)
EXPERIENCE_IGNORE_CONTEXT = re.compile(
    r"\b(?:years\s+of\s+age|years\s+old|combined\s+experience|for\s+over\s+\d+\s+years|"
    r"founded\s+\d+\s+years|celebrating\s+\d+\s+years)\b",
    re.IGNORECASE,
)

DEFAULT_MAX_EXPERIENCE_YEARS = 5


def extract_min_years_experience(description_text: Optional[str]) -> Optional[int]:
    """Extract the minimum required years of experience from a job description.

    When a posting lists alternative degree tiers (e.g. '5+ years with BS or 3+ years with MS'),
    or multiple skill durations, returns the minimum general requirement in [1, 15].
    Returns None if no explicit years-of-experience requirement is found.
    """
    if not description_text:
        return None

    found_years: list[int] = []
    for line in description_text.splitlines():
        if EXPERIENCE_IGNORE_CONTEXT.search(line):
            continue
        for pattern in EXPERIENCE_PATTERNS:
            for match in pattern.finditer(line):
                try:
                    yrs = int(match.group(1))
                except (ValueError, IndexError):
                    continue
                # Reasonable candidate experience requirement bounds (1 to 15 years)
                if 1 <= yrs <= 15:
                    found_years.append(yrs)

    if not found_years:
        return None
    return min(found_years)


def classify_experience_tier(
    min_years: Optional[int],
    title: str = "",
    primary_max: int = 3,
    stretch_max: int = 5,
) -> str:
    """Classify a posting's experience requirement into Primary (1-3 yrs), Stretch (3-5 yrs), or Knockout."""
    if min_years is not None:
        if min_years <= primary_max:
            return f"{min_years}+ yrs (Primary 1-3)"
        if min_years <= stretch_max:
            return f"{min_years}+ yrs (Stretch 3-5)"
        return f"{min_years}+ yrs (Exceeds 5)"

    if SENIOR_TITLE_PATTERN.search(title or ""):
        return "Unspecified (Sr. Stretch)"
    return "Unspecified (Target)"


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
    for block_tag in soup.find_all(["p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr"]):
        block_tag.append("\n")
    text = soup.get_text(separator=" ")
    # Collapse excessive spaces and blank lines
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


def classify_location(
    location_raw: Optional[str],
    allow_dc_fort_meade: bool = False,
) -> Optional[WorkplaceType]:
    """Evaluate location against Local (Raleigh/RTP <=25 min, plus DC/Fort Meade when allow_dc_fort_meade=True) and US-Remote rules.

    Returns:
        'Local', 'Hybrid', or 'Remote' if the location passes the filter;
        None if the location should be rejected.
    """
    if not location_raw or not location_raw.strip():
        return None

    loc = location_raw.strip()

    # Check Mode 2 first: Local Raleigh/RTP commute radius (or USAJOBS DC / Fort Meade corridor)
    if LOCAL_RTP_PATTERN.search(loc) or (
        allow_dc_fort_meade and USAJOBS_DC_FORT_MEADE_PATTERN.search(loc)
    ):
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


def has_non_local_onsite_requirement(description_text: Optional[str]) -> bool:
    """Return True if a 'Remote' job's description explicitly mandates non-local hybrid/on-site attendance."""
    if not description_text:
        return False
    for match in MANDATORY_ONSITE_HYBRID_PATTERN.finditer(description_text):
        # Check a +/- 120 char window around the mandate; if it's hybrid in Raleigh/RTP, allow it
        start = max(0, match.start() - 120)
        end = min(len(description_text), match.end() + 120)
        context = description_text[start:end]
        if not LOCAL_RTP_PATTERN.search(context):
            return True
    return False


def evaluate_posting(
    company: str,
    title: str,
    location_raw: Optional[str],
    description_text: Optional[str] = None,
    max_experience_years: int = DEFAULT_MAX_EXPERIENCE_YEARS,
    allow_dc_fort_meade: bool = False,
) -> Tuple[bool, Optional[WorkplaceType], Optional[str], str]:
    """Run all Step 3.1 Hard Knockout Filters (including max years of experience) on a job posting.

    Returns:
        Tuple of (passed, workplace_type, job_hash, reason)
    """
    if is_excluded_title(title):
        return False, None, None, "Excluded seniority/prefix in title"

    if not is_target_title(title):
        return False, None, None, "Title outside target role families"

    workplace_type = classify_location(location_raw, allow_dc_fort_meade=allow_dc_fort_meade)
    if workplace_type is None:
        return False, None, None, f"Location rejected ({location_raw!r})"

    if description_text:
        if workplace_type == "Remote" and has_non_local_onsite_requirement(description_text):
            return (
                False,
                None,
                None,
                "Fake remote rejected: description requires non-local hybrid/on-site attendance",
            )

        min_years = extract_min_years_experience(description_text)
        if min_years is not None and min_years > max_experience_years:
            return False, None, None, f"Requires {min_years}+ years experience (max {max_experience_years})"

    job_hash = compute_job_hash(company, title, workplace_type)
    return True, workplace_type, job_hash, "Passed"
