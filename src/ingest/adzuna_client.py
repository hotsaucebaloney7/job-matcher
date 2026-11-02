"""Adzuna API aggregator client for broad Local (Raleigh/RTP 24km radius) and US-Remote job search."""

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

ADZUNA_BASE_URL = "https://api.adzuna.com/v1/api/jobs/us/search/{page}"
REQUEST_TIMEOUT_SECONDS = 15

# Local Raleigh/RTP query (distance=24 km ~= 15 miles / <= 25 min commute)
LOCAL_QUERY_PARAMS: Dict[str, Any] = {
    "what_or": "Data Scientist AI Engineer Machine Learning Engineer Data Engineer MLOps",
    "where": "Raleigh, NC",
    "distance": 24,
    "results_per_page": 50,
    "content-type": "application/json",
}

# Broad US-Remote query for target titles
REMOTE_QUERY_PARAMS: Dict[str, Any] = {
    "what_or": "Data Scientist AI Engineer Machine Learning Engineer",
    "what": "remote",
    "results_per_page": 50,
    "content-type": "application/json",
}


def is_adzuna_configured(app_id: Optional[str] = None, api_key: Optional[str] = None) -> bool:
    """Return True if Adzuna API credentials are provided or present in environment."""
    resolved_id = (app_id if app_id is not None else os.getenv("ADZUNA_APP_ID", "")).strip()
    resolved_key = (api_key if api_key is not None else os.getenv("ADZUNA_API_KEY", "")).strip()
    return bool(resolved_id and resolved_key)


def _parse_adzuna_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def _execute_adzuna_query(
    session: requests.Session,
    app_id: str,
    api_key: str,
    query_params: Dict[str, Any],
    default_location_hint: str = "",
    page: int = 1,
) -> Tuple[List[Dict[str, Any]], int]:
    """Run a single Adzuna search query and filter/normalize results."""
    params = {
        "app_id": app_id,
        "app_key": api_key,
        **query_params,
    }
    url = ADZUNA_BASE_URL.format(page=page)
    resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    payload = resp.json()
    raw_results = payload.get("results", []) or []

    matched: List[Dict[str, Any]] = []
    for item in raw_results:
        title = strip_html(item.get("title") or "").strip()
        company_obj = item.get("company") or {}
        company = (company_obj.get("display_name") or "Unknown Company").strip()

        loc_obj = item.get("location") or {}
        display_loc = (loc_obj.get("display_name") or "").strip()
        area_list = loc_obj.get("area") or []
        area_str = ", ".join(str(a) for a in area_list if a)
        location_raw = " | ".join(
            part for part in [display_loc, area_str, default_location_hint] if part
        )
        description_text = strip_html(item.get("description") or "")

        passed, workplace_type, job_hash, _ = evaluate_posting(
            company=company,
            title=title,
            location_raw=location_raw,
            description_text=description_text,
        )
        if not passed:
            continue

        salary_min = item.get("salary_min")
        salary_max = item.get("salary_max")

        matched.append(
            {
                "job_hash": job_hash,
                "source": "adzuna",
                "external_id": str(item.get("id")) if item.get("id") is not None else None,
                "company": company,
                "title": title,
                "location_raw": display_loc or location_raw,
                "workplace_type": workplace_type,
                "salary_min": int(salary_min) if isinstance(salary_min, (int, float)) else None,
                "salary_max": int(salary_max) if isinstance(salary_max, (int, float)) else None,
                "url": item.get("redirect_url") or "",
                "description_text": description_text,
                "date_posted": _parse_adzuna_date(item.get("created")),
            }
        )

    return matched, len(raw_results)


def poll_adzuna(
    app_id: Optional[str] = None,
    api_key: Optional[str] = None,
    console: Optional[Console] = None,
    session: Optional[requests.Session] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Poll Adzuna Local (Raleigh 24km) and Remote queries when credentials are configured.

    Returns:
        Tuple of (deduplicated_matched_jobs, query_stats)
    """
    resolved_id = (app_id if app_id is not None else os.getenv("ADZUNA_APP_ID", "")).strip()
    resolved_key = (api_key if api_key is not None else os.getenv("ADZUNA_API_KEY", "")).strip()

    if not (resolved_id and resolved_key):
        if console:
            console.print(
                "[dim]Source B (Adzuna): Skipped (ADZUNA_APP_ID / ADZUNA_API_KEY not set in .env)[/dim]"
            )
        return [], []

    deduped: Dict[str, Dict[str, Any]] = {}
    stats: List[Dict[str, Any]] = []

    own_session = session is None
    http = session or requests.Session()
    try:
        # Query 1: Local Raleigh/RTP 24km radius
        try:
            local_matches, local_raw = _execute_adzuna_query(
                http,
                resolved_id,
                resolved_key,
                LOCAL_QUERY_PARAMS,
                default_location_hint="",
            )
            for job in local_matches:
                deduped.setdefault(job["job_hash"], job)
            stats.append(
                {
                    "ats": "Adzuna",
                    "company": "Local (Raleigh 24km)",
                    "raw": local_raw,
                    "matched": len(local_matches),
                    "status": "OK",
                }
            )
        except Exception as exc:
            stats.append(
                {
                    "ats": "Adzuna",
                    "company": "Local (Raleigh 24km)",
                    "raw": 0,
                    "matched": 0,
                    "status": f"ERROR: {exc}",
                }
            )
            if console:
                console.print(f"[yellow]Warning:[/yellow] Adzuna local query failed: {exc}")

        # Query 2: US Remote
        try:
            remote_matches, remote_raw = _execute_adzuna_query(
                http,
                resolved_id,
                resolved_key,
                REMOTE_QUERY_PARAMS,
                default_location_hint="Remote, US",
            )
            for job in remote_matches:
                deduped.setdefault(job["job_hash"], job)
            stats.append(
                {
                    "ats": "Adzuna",
                    "company": "Remote (US)",
                    "raw": remote_raw,
                    "matched": len(remote_matches),
                    "status": "OK",
                }
            )
        except Exception as exc:
            stats.append(
                {
                    "ats": "Adzuna",
                    "company": "Remote (US)",
                    "raw": 0,
                    "matched": 0,
                    "status": f"ERROR: {exc}",
                }
            )
            if console:
                console.print(f"[yellow]Warning:[/yellow] Adzuna remote query failed: {exc}")
    finally:
        if own_session:
            http.close()

    return list(deduped.values()), stats
