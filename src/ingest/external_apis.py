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
        {"label": "Raleigh / Durham, NC", "location": ["Raleigh, NC", "Durham, NC"]},
        {"label": "Flexible / Remote", "location": ["Flexible / Remote"]},
    ]
    categories = ["Data and Analytics", "Data Science"]
    levels = ["Entry Level", "Mid Level"]

    deduped: Dict[str, Dict[str, Any]] = {}
    stats: List[Dict[str, Any]] = []

    own_session = session is None
    http = session or requests.Session()
    http.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    try:
        for q in queries:
            params: Dict[str, Any] = {
                "page": 1,
                "category": categories,
                "level": levels,
                "location": q["location"],
            }
            if resolved_key:
                params["api_key"] = resolved_key

            try:
                resp = http.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
                resp.raise_for_status()
                raw_jobs = (resp.json() or {}).get("results", []) or []

                matched_count = 0
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
                    deduped.setdefault(
                        job_hash,
                        {
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
                        },
                    )
                    matched_count += 1

                stats.append(
                    {
                        "ats": "The Muse",
                        "company": q["label"],
                        "raw": len(raw_jobs),
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

    url = "https://jsearch.p.rapidapi.com/search"
    headers = {
        "X-RapidAPI-Key": resolved_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }
    queries = [
        {
            "label": "Raleigh / RTP Local",
            "params": {
                "query": "Data Scientist OR AI Engineer OR Machine Learning Engineer in Raleigh, NC",
                "page": "1",
                "num_pages": "1",
                "date_posted": "week",
            },
        },
        {
            "label": "US Remote",
            "params": {
                "query": "Data Scientist OR AI Engineer OR Machine Learning Engineer in USA",
                "remote_jobs_only": "true",
                "page": "1",
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
                resp = http.get(url, headers=headers, params=q["params"], timeout=REQUEST_TIMEOUT_SECONDS)
                resp.raise_for_status()
                raw_jobs = (resp.json() or {}).get("data", []) or []

                matched_count = 0
                for item in raw_jobs:
                    title = (item.get("job_title") or "").strip()
                    company = (item.get("employer_name") or "Unknown Company").strip()
                    city = (item.get("job_city") or "").strip()
                    state = (item.get("job_state") or "").strip()
                    country = (item.get("job_country") or "").strip()
                    is_remote = item.get("job_is_remote") is True

                    loc_parts = [p for p in [city, state, country] if p]
                    if is_remote:
                        loc_parts.insert(0, "Remote")
                    location_raw = ", ".join(loc_parts)
                    description_text = strip_html(item.get("job_description") or "")

                    # Check structured JSearch experience months if present
                    exp_obj = item.get("job_required_experience") or {}
                    req_months = exp_obj.get("required_experience_in_months")
                    if isinstance(req_months, (int, float)) and req_months >= 72:
                        # 72+ months = 6+ years -> knockout
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

                    deduped.setdefault(
                        job_hash,
                        {
                            "job_hash": job_hash,
                            "source": "jsearch",
                            "external_id": str(item.get("job_id")) if item.get("job_id") else None,
                            "company": company,
                            "title": title,
                            "location_raw": location_raw,
                            "workplace_type": workplace_type,
                            "salary_min": int(sal_min) if isinstance(sal_min, (int, float)) else None,
                            "salary_max": int(sal_max) if isinstance(sal_max, (int, float)) else None,
                            "url": item.get("job_apply_link") or item.get("job_google_link") or "",
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
