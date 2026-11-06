"""Additional Aggregator & Job Board Clients: Remotive (Zero-Key), The Muse, JSearch (Google for Jobs), and USAJOBS."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from rich.console import Console

from src.scoring.filters import evaluate_posting, strip_html

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=ENV_PATH)

REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = "JobMatcher/1.0 (+local-personal-job-search)"

BLOCKED_SCRAPER_DOMAINS = (
    "jobleads.com",
    "trabajo.org",
    "bebee.com",
    "jobrapido.com",
    "learn4good.com",
    "jobmesh.io",
    "lensa.com",
)


def _is_blocked_scraper_url(url: str) -> bool:
    u_low = (url or "").lower()
    return any(domain in u_low for domain in BLOCKED_SCRAPER_DOMAINS)


def _select_best_jsearch_apply_url(item: Dict[str, Any]) -> Optional[str]:
    """Select the highest-trust apply URL from JSearch, preferring direct employer links."""
    options = item.get("apply_options") or []
    if isinstance(options, list):
        # 1. Prefer direct employer ATS / career site links
        for opt in options:
            if isinstance(opt, dict) and opt.get("is_direct") is True:
                link = (opt.get("apply_link") or "").strip()
                if link and not _is_blocked_scraper_url(link):
                    return link[:768]
        # 2. Next, accept reputable boards that aren't paywall/stale scraper sites
        for opt in options:
            if isinstance(opt, dict):
                link = (opt.get("apply_link") or "").strip()
                if link and not _is_blocked_scraper_url(link):
                    return link[:768]

    fallback = (item.get("job_apply_link") or item.get("job_google_link") or "").strip()
    if fallback and not _is_blocked_scraper_url(fallback):
        return fallback[:768]
    return None


def _parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def poll_remotive(
    session: Optional[requests.Session] = None,
    console: Optional[Console] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Poll Remotive's zero-key public JSON API for Remote Data & AI/ML jobs."""
    categories = ["data", "ai-ml"]
    deduped: Dict[str, Dict[str, Any]] = {}
    stats: List[Dict[str, Any]] = []

    own_session = session is None
    http = session or requests.Session()
    http.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    try:
        for cat in categories:
            url = f"https://remotive.com/api/remote-jobs?category={cat}&limit=100"
            try:
                resp = http.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
                resp.raise_for_status()
                raw_jobs = (resp.json() or {}).get("jobs", []) or []

                matched_count = 0
                for item in raw_jobs:
                    title = (item.get("title") or "").strip()
                    company = (item.get("company_name") or "Unknown Company").strip()
                    candidate_loc = (item.get("candidate_required_location") or "Remote").strip()
                    location_raw = f"Remote ({candidate_loc})"
                    description_text = strip_html(item.get("description") or "")

                    passed, workplace_type, job_hash, _ = evaluate_posting(
                        company=company,
                        title=title,
                        location_raw=location_raw,
                        description_text=description_text,
                    )
                    if not passed:
                        continue

                    deduped.setdefault(
                        job_hash,
                        {
                            "job_hash": job_hash,
                            "source": "remotive",
                            "external_id": str(item.get("id")) if item.get("id") is not None else None,
                            "company": company,
                            "title": title,
                            "location_raw": location_raw,
                            "workplace_type": workplace_type,
                            "salary_min": None,
                            "salary_max": None,
                            "url": item.get("url") or "",
                            "description_text": description_text,
                            "date_posted": _parse_iso_dt(item.get("publication_date")),
                        },
                    )
                    matched_count += 1

                stats.append(
                    {
                        "ats": "Remotive",
                        "company": f"Category: {cat}",
                        "raw": len(raw_jobs),
                        "matched": matched_count,
                        "status": "OK",
                    }
                )
            except Exception as exc:
                stats.append(
                    {
                        "ats": "Remotive",
                        "company": f"Category: {cat}",
                        "raw": 0,
                        "matched": 0,
                        "status": f"ERROR: {exc}",
                    }
                )
                if console:
                    console.print(f"[yellow]Warning:[/yellow] Remotive ({cat}) failed: {exc}")
    finally:
        if own_session:
            http.close()

    return list(deduped.values()), stats


def poll_the_muse(
    api_key: Optional[str] = None,
    session: Optional[requests.Session] = None,
    console: Optional[Console] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Poll The Muse public API for Entry/Mid-Level Data & AI roles in Raleigh/RTP and Remote.

    Works both without an API key (public rate limit) and with THE_MUSE_API_KEY.
    """
    resolved_key = (api_key if api_key is not None else os.getenv("THE_MUSE_API_KEY", "")).strip()
    url = "https://www.themuse.com/api/public/jobs"

    queries = [
        {"label": "Raleigh / Durham / Cary, NC", "location": ["Raleigh, NC", "Durham, NC", "Cary, NC"]},
        {"label": "Flexible / Remote", "location": ["Flexible / Remote"]},
    ]
    categories = ["Data and Analytics", "Data Science"]
    levels = ["Entry Level", "Mid Level", "Senior Level"]
    max_pages = 5 if resolved_key else 1

    deduped: Dict[str, Dict[str, Any]] = {}
    stats: List[Dict[str, Any]] = []

    own_session = session is None
    http = session or requests.Session()
    http.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    try:
        for q in queries:
            raw_total = 0
            matched_count = 0
            try:
                for page in range(1, max_pages + 1):
                    params: Dict[str, Any] = {
                        "page": page,
                        "category": categories,
                        "level": levels,
                        "location": q["location"],
                    }
                    if resolved_key:
                        params["api_key"] = resolved_key

                    resp = http.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
                    resp.raise_for_status()
                    payload = resp.json() or {}
                    raw_jobs = payload.get("results", []) or []
                    if not raw_jobs:
                        break
                    raw_total += len(raw_jobs)

                    for item in raw_jobs:
                        title = (item.get("name") or "").strip()
                        company = ((item.get("company") or {}).get("name") or "Unknown Company").strip()
                        locs = [
                            (loc.get("name") or "").strip()
                            for loc in (item.get("locations") or [])
                            if isinstance(loc, dict) and loc.get("name")
                        ]
                        location_raw = " | ".join(locs)
                        description_text = strip_html(item.get("contents") or "")

                        passed, workplace_type, job_hash, _ = evaluate_posting(
                            company=company,
                            title=title,
                            location_raw=location_raw,
                            description_text=description_text,
                        )
                        if not passed:
                            continue

                        refs = item.get("refs") or {}
                        if job_hash not in deduped:
                            deduped[job_hash] = {
                                "job_hash": job_hash,
                                "source": "themuse",
                                "external_id": str(item.get("id")) if item.get("id") is not None else None,
                                "company": company,
                                "title": title,
                                "location_raw": location_raw,
                                "workplace_type": workplace_type,
                                "salary_min": None,
                                "salary_max": None,
                                "url": refs.get("landing_page") or "",
                                "description_text": description_text,
                                "date_posted": _parse_iso_dt(item.get("publication_date")),
                            }
                            matched_count += 1

                    page_count = payload.get("page_count")
                    if isinstance(page_count, int) and page >= page_count:
                        break

                stats.append(
                    {
                        "ats": "The Muse",
                        "company": q["label"],
                        "raw": raw_total,
                        "matched": matched_count,
                        "status": "OK",
                    }
                )
            except Exception as exc:
                stats.append(
                    {
                        "ats": "The Muse",
                        "company": q["label"],
                        "raw": 0,
                        "matched": 0,
                        "status": f"ERROR: {exc}",
                    }
                )
                if console:
                    console.print(f"[yellow]Warning:[/yellow] The Muse ({q['label']}) failed: {exc}")
    finally:
        if own_session:
            http.close()

    return list(deduped.values()), stats


def poll_jsearch_rapidapi(
    rapidapi_key: Optional[str] = None,
    session: Optional[requests.Session] = None,
    console: Optional[Console] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Poll JSearch (Google for Jobs aggregator on RapidAPI) when JSEARCH_RAPIDAPI_KEY is set in .env."""
    resolved_key = (
        rapidapi_key if rapidapi_key is not None else os.getenv("JSEARCH_RAPIDAPI_KEY", "")
    ).strip()
    if not resolved_key:
        if console:
            console.print("[dim]JSearch (Google for Jobs): Skipped (JSEARCH_RAPIDAPI_KEY not set in .env)[/dim]")
        return [], []

    url = "https://jsearch.p.rapidapi.com/search-v2"
    headers = {
        "X-RapidAPI-Key": resolved_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }
    queries = [
        {
            "label": "Raleigh / RTP Local (Data Scientist)",
            "params": {
                "query": "Data Scientist in Raleigh, NC",
                "num_pages": "1",
                "date_posted": "week",
            },
        },
        {
            "label": "Raleigh / RTP Local (AI / ML Engineer)",
            "params": {
                "query": "AI Engineer Machine Learning Engineer in Raleigh, NC",
                "num_pages": "1",
                "date_posted": "week",
            },
        },
        {
            "label": "US Remote (Data Scientist / AI Engineer)",
            "params": {
                "query": "Remote Data Scientist AI Engineer in USA",
                "work_from_home": "true",
                "num_pages": "1",
                "date_posted": "week",
            },
        },
    ]

    deduped: Dict[str, Dict[str, Any]] = {}
    stats: List[Dict[str, Any]] = []

    own_session = session is None
    http = session or requests.Session()

    try:
        for q in queries:
            try:
                resp = http.get(url, headers=headers, params=q["params"], timeout=30)
                resp.raise_for_status()
                data_obj = (resp.json() or {}).get("data")
                if isinstance(data_obj, dict):
                    raw_jobs = data_obj.get("jobs", []) or []
                elif isinstance(data_obj, list):
                    raw_jobs = data_obj
                else:
                    raw_jobs = []

                matched_count = 0
                for item in raw_jobs:
                    title = (item.get("job_title") or "").strip()
                    company = (item.get("employer_name") or "Unknown Company").strip()
                    job_loc = (item.get("job_location") or "").strip()
                    city = (item.get("job_city") or "").strip()
                    state = (item.get("job_state") or "").strip()
                    country = (item.get("job_country") or "").strip()
                    is_remote = item.get("job_is_remote") is True

                    loc_parts = [p for p in [job_loc, city, state, country] if p]
                    if is_remote:
                        loc_parts.insert(0, "Remote")
                    location_raw = ", ".join(dict.fromkeys(loc_parts))
                    description_text = strip_html(item.get("job_description") or "")

                    # Check structured JSearch experience months if present
                    exp_obj = item.get("job_required_experience") or {}
                    req_months = exp_obj.get("required_experience_in_months")
                    if isinstance(req_months, (int, float)) and req_months >= 72:
                        # 72+ months = 6+ years -> knockout
                        continue

                    # Skip if JSearch metadata indicates the offer has already expired
                    exp_dt = _parse_iso_dt(item.get("job_offer_expiration_datetime_utc"))
                    if exp_dt is not None and exp_dt < datetime.utcnow():
                        continue

                    apply_url = _select_best_jsearch_apply_url(item)
                    if not apply_url:
                        continue

                    passed, workplace_type, job_hash, _ = evaluate_posting(
                        company=company,
                        title=title,
                        location_raw=location_raw,
                        description_text=description_text,
                    )
                    if not passed:
                        continue

                    sal_min = item.get("job_min_salary")
                    sal_max = item.get("job_max_salary")
                    period = (item.get("job_salary_period") or "").upper()
                    if period not in ("YEAR", "YEARLY", ""):
                        sal_min, sal_max = None, None

                    raw_id = str(item.get("job_id") or "")
                    deduped.setdefault(
                        job_hash,
                        {
                            "job_hash": job_hash,
                            "source": "jsearch",
                            "external_id": raw_id[:128] if raw_id else None,
                            "company": company,
                            "title": title,
                            "location_raw": job_loc or location_raw,
                            "workplace_type": workplace_type,
                            "salary_min": int(sal_min) if isinstance(sal_min, (int, float)) else None,
                            "salary_max": int(sal_max) if isinstance(sal_max, (int, float)) else None,
                            "url": apply_url,
                            "description_text": description_text,
                            "date_posted": _parse_iso_dt(item.get("job_posted_at_datetime_utc")),
                        },
                    )
                    matched_count += 1

                stats.append(
                    {
                        "ats": "JSearch",
                        "company": q["label"],
                        "raw": len(raw_jobs),
                        "matched": matched_count,
                        "status": "OK",
                    }
                )
            except Exception as exc:
                stats.append(
                    {
                        "ats": "JSearch",
                        "company": q["label"],
                        "raw": 0,
                        "matched": 0,
                        "status": f"ERROR: {exc}",
                    }
                )
                if console:
                    console.print(f"[yellow]Warning:[/yellow] JSearch ({q['label']}) failed: {exc}")
    finally:
        if own_session:
            http.close()

    return list(deduped.values()), stats


def poll_usajobs(
    api_key: Optional[str] = None,
    user_email: Optional[str] = None,
    session: Optional[requests.Session] = None,
    console: Optional[Console] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Poll the official USAJOBS REST API for Local (Raleigh/RTP) and US-Remote Data/AI roles.

    Requires `USAJOBS_API_KEY` (and `USAJOBS_USER_EMAIL` as User-Agent header) in `.env`.
    """
    resolved_key = (api_key if api_key is not None else os.getenv("USAJOBS_API_KEY", "")).strip()
    resolved_email = (
        user_email
        if user_email is not None
        else os.getenv("USAJOBS_USER_EMAIL", "mrmackelroy@gmail.com")
    ).strip()

    if not resolved_key:
        if console:
            console.print("[dim]USAJOBS: Skipped (USAJOBS_API_KEY not set in .env)[/dim]")
        return [], []

    url = "https://data.usajobs.gov/api/Search"
    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": resolved_email,
        "Authorization-Key": resolved_key,
        "Accept": "application/json",
    }

    queries = [
        {
            "label": "Raleigh / RTP (25 mi)",
            "params": {
                "LocationName": "Raleigh, North Carolina",
                "Radius": "25",
                "JobCategoryCode": "1560;1550;1515;1529;1530;2210;0854",
                "ResultsPerPage": "100",
            },
            "is_remote_query": False,
        },
        {
            "label": "Washington DC / DMV (25 mi)",
            "params": {
                "LocationName": "Washington, District of Columbia",
                "Radius": "25",
                "JobCategoryCode": "1560;1550;1515;1529;1530;2210;0854",
                "ResultsPerPage": "100",
            },
            "is_remote_query": False,
        },
        {
            "label": "Fort Meade / MD Corridor (25 mi)",
            "params": {
                "LocationName": "Fort George G. Meade, Maryland",
                "Radius": "25",
                "JobCategoryCode": "1560;1550;1515;1529;1530;2210;0854",
                "ResultsPerPage": "100",
            },
            "is_remote_query": False,
        },
        {
            "label": "Federal Data Scientist (1560)",
            "params": {
                "Keyword": "Data Scientist",
                "ResultsPerPage": "100",
            },
            "is_remote_query": False,
        },
        {
            "label": "Federal AI / Machine Learning",
            "params": {
                "Keyword": "Machine Learning",
                "ResultsPerPage": "100",
            },
            "is_remote_query": False,
        },
    ]

    deduped: Dict[str, Dict[str, Any]] = {}
    stats: List[Dict[str, Any]] = []

    own_session = session is None
    http = session or requests.Session()

    try:
        for q in queries:
            try:
                resp = http.get(url, headers=headers, params=q["params"], timeout=REQUEST_TIMEOUT_SECONDS)
                resp.raise_for_status()
                search_result = (resp.json() or {}).get("SearchResult") or {}
                raw_items = search_result.get("SearchResultItems") or []

                matched_count = 0
                for item in raw_items:
                    desc = item.get("MatchedObjectDescriptor") or {}
                    title = (desc.get("PositionTitle") or "").strip()
                    company = (
                        desc.get("OrganizationName")
                        or desc.get("DepartmentName")
                        or "U.S. Federal Government"
                    ).strip()

                    loc_display = (desc.get("PositionLocationDisplay") or "").strip()
                    pos_locs = [
                        (loc.get("LocationName") or "").strip()
                        for loc in (desc.get("PositionLocation") or [])
                        if isinstance(loc, dict) and loc.get("LocationName")
                    ]
                    # Keep any North Carolina / Triangle or DC / Fort Meade / MD / VA duty stations plus first 3 locations
                    priority_locs = [
                        l
                        for l in pos_locs
                        if any(
                            k in l.lower()
                            for k in (
                                "north carolina",
                                ", nc",
                                "raleigh",
                                "durham",
                                "cary",
                                "morrisville",
                                "district of columbia",
                                "washington",
                                "meade",
                                "maryland",
                                ", md",
                                "virginia",
                                ", va",
                                "annapolis junction",
                                "laurel",
                                "columbia",
                                "baltimore",
                                "arlington",
                                "alexandria",
                                "mclean",
                                "reston",
                                "chantilly",
                                "fort belvoir",
                                "bethesda",
                                "silver spring",
                                "suitland",
                                "greenbelt",
                                "adelphi",
                            )
                        )
                    ]
                    selected_locs = list(dict.fromkeys(priority_locs + pos_locs[:3]))
                    loc_parts = [p for p in [loc_display, ", ".join(selected_locs)] if p]
                    if q["is_remote_query"] or "anywhere in the u.s." in loc_display.lower():
                        loc_parts.insert(0, "Remote - United States")
                    location_raw = " | ".join(dict.fromkeys(loc_parts))

                    details = (desc.get("UserArea") or {}).get("Details") or {}
                    summary = details.get("JobSummary") or ""
                    duties = details.get("MajorDuties") or []
                    if isinstance(duties, list):
                        duties_str = "\n".join(str(d) for d in duties)
                    else:
                        duties_str = str(duties)
                    quals = desc.get("QualificationSummary") or ""
                    description_text = strip_html(f"{summary}\n{duties_str}\n{quals}")

                    passed, workplace_type, job_hash, _ = evaluate_posting(
                        company=company,
                        title=title,
                        location_raw=location_raw,
                        description_text=description_text,
                        allow_dc_fort_meade=True,
                    )
                    if not passed:
                        continue

                    display_loc = loc_display
                    if (
                        "location negotiable" in loc_display.lower()
                        or "multiple locations" in loc_display.lower()
                    ) and priority_locs:
                        display_loc = f"{loc_display} ({', '.join(priority_locs[:2])})"

                    salary_min: Optional[int] = None
                    salary_max: Optional[int] = None
                    for rem in (desc.get("PositionRemuneration") or []):
                        interval = (rem.get("RateIntervalCode") or "").upper()
                        if interval in ("PA", "PER YEAR", "ANNUAL", ""):
                            try:
                                low = float(rem.get("MinimumRange") or 0)
                                high = float(rem.get("MaximumRange") or 0)
                                if low >= 30000:
                                    salary_min = int(low)
                                if high >= 30000:
                                    salary_max = int(high)
                            except (ValueError, TypeError):
                                pass

                    apply_uris = desc.get("ApplyURI") or []
                    job_url = desc.get("PositionURI") or (apply_uris[0] if apply_uris else "")

                    deduped.setdefault(
                        job_hash,
                        {
                            "job_hash": job_hash,
                            "source": "usajobs",
                            "external_id": str(item.get("MatchedObjectId") or ""),
                            "company": company,
                            "title": title,
                            "location_raw": display_loc or location_raw,
                            "workplace_type": workplace_type,
                            "salary_min": salary_min,
                            "salary_max": salary_max,
                            "url": job_url,
                            "description_text": description_text,
                            "date_posted": _parse_iso_dt(desc.get("PublicationStartDate")),
                        },
                    )
                    matched_count += 1

                stats.append(
                    {
                        "ats": "USAJOBS",
                        "company": q["label"],
                        "raw": len(raw_items),
                        "matched": matched_count,
                        "status": "OK",
                    }
                )
            except Exception as exc:
                stats.append(
                    {
                        "ats": "USAJOBS",
                        "company": q["label"],
                        "raw": 0,
                        "matched": 0,
                        "status": f"ERROR: {exc}",
                    }
                )
                if console:
                    console.print(f"[yellow]Warning:[/yellow] USAJOBS ({q['label']}) failed: {exc}")
    finally:
        if own_session:
            http.close()

    return list(deduped.values()), stats
