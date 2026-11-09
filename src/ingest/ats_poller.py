"""Direct ATS Poller for Greenhouse and Lever public JSON job boards."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET

import requests
import yaml
from rich.console import Console
from rich.table import Table

from src.scoring.filters import (
    classify_location,
    evaluate_posting,
    is_excluded_title,
    is_target_title,
    strip_html,
)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "companies.yaml"
REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = "JobMatcher/1.0 (+local-personal-job-search)"
WORKDAY_SEARCH_TERMS = ("Data Scientist", "Data Engineer", "Machine Learning", "AI Engineer")


def load_companies_config(config_path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, List[Dict[str, str]]]:
    """Load Greenhouse, Lever, Ashby, Workday CXS, and XML feed definitions from YAML."""
    if not config_path.exists():
        raise FileNotFoundError(f"Companies configuration file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {
        "greenhouse": data.get("greenhouse", []) or [],
        "lever": data.get("lever", []) or [],
        "ashby": data.get("ashby", []) or [],
        "workday": data.get("workday", []) or [],
        "xml_feeds": data.get("xml_feeds", []) or [],
    }


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 timestamp string into a naive UTC datetime for MySQL storage."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def _parse_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    """Parse Lever millisecond epoch timestamp into a naive UTC datetime."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, OSError, TypeError):
        return None


def fetch_greenhouse_board(slug: str, company_name: str, session: requests.Session) -> Tuple[List[Dict[str, Any]], int]:
    """Fetch and filter jobs from a public Greenhouse board.

    Returns:
        Tuple of (matched_postings, total_raw_jobs_seen)
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    resp = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    payload = resp.json()
    raw_jobs = payload.get("jobs", []) or []

    matched: List[Dict[str, Any]] = []
    for item in raw_jobs:
        title = (item.get("title") or "").strip()
        loc_obj = item.get("location") or {}
        location_raw = (loc_obj.get("name") or "").strip()
        description_text = strip_html(item.get("content") or "")

        passed, workplace_type, job_hash, _ = evaluate_posting(
            company=company_name,
            title=title,
            location_raw=location_raw,
            description_text=description_text,
        )
        if not passed:
            continue

        date_posted = _parse_iso_datetime(item.get("first_published") or item.get("updated_at"))

        matched.append(
            {
                "job_hash": job_hash,
                "source": "greenhouse",
                "external_id": str(item.get("id")) if item.get("id") is not None else None,
                "company": company_name,
                "title": title,
                "location_raw": location_raw,
                "workplace_type": workplace_type,
                "salary_min": None,
                "salary_max": None,
                "url": item.get("absolute_url") or "",
                "description_text": description_text,
                "date_posted": date_posted,
            }
        )

    return matched, len(raw_jobs)


def fetch_lever_board(slug: str, company_name: str, session: requests.Session) -> Tuple[List[Dict[str, Any]], int]:
    """Fetch and filter jobs from a public Lever board.

    Returns:
        Tuple of (matched_postings, total_raw_jobs_seen)
    """
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    raw_jobs = resp.json()
    if not isinstance(raw_jobs, list):
        return [], 0

    matched: List[Dict[str, Any]] = []
    for item in raw_jobs:
        title = (item.get("text") or "").strip()
        categories = item.get("categories") or {}
        all_locations = categories.get("allLocations") or []
        primary_loc = (categories.get("location") or "").strip()
        workplace_hint = (item.get("workplaceType") or "").strip()

        # Combine primary location, allLocations, and workplaceType hint so remote/local tags aren't missed
        loc_parts = [p for p in [primary_loc, ", ".join(all_locations), workplace_hint] if p]
        location_raw = " | ".join(dict.fromkeys(loc_parts))

        desc_plain = item.get("descriptionPlain") or strip_html(item.get("description") or "")
        lists_html = "\n".join(
            f"{lst.get('text', '')}\n{lst.get('content', '')}" for lst in (item.get("lists") or [])
        )
        additional_plain = item.get("additionalPlain") or strip_html(item.get("additional") or "")
        full_description = "\n\n".join(
            part for part in [desc_plain, strip_html(lists_html), additional_plain] if part
        )

        passed, workplace_type, job_hash, _ = evaluate_posting(
            company=company_name,
            title=title,
            location_raw=location_raw,
            description_text=full_description,
        )
        if not passed:
            continue

        salary_range = item.get("salaryRange") or {}
        salary_min = salary_range.get("min")
        salary_max = salary_range.get("max")

        matched.append(
            {
                "job_hash": job_hash,
                "source": "lever",
                "external_id": str(item.get("id")) if item.get("id") is not None else None,
                "company": company_name,
                "title": title,
                "location_raw": location_raw,
                "workplace_type": workplace_type,
                "salary_min": int(salary_min) if isinstance(salary_min, (int, float)) else None,
                "salary_max": int(salary_max) if isinstance(salary_max, (int, float)) else None,
                "url": item.get("hostedUrl") or item.get("applyUrl") or "",
                "description_text": full_description,
                "date_posted": _parse_epoch_ms(item.get("createdAt")),
            }
        )

    return matched, len(raw_jobs)


def fetch_ashby_board(slug: str, company_name: str, session: requests.Session) -> Tuple[List[Dict[str, Any]], int]:
    """Fetch and filter jobs from a public Ashby job board API.

    Endpoint:
        https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    resp = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    payload = resp.json() or {}
    raw_jobs = payload.get("jobs", []) or []

    matched: List[Dict[str, Any]] = []
    for item in raw_jobs:
        if item.get("isListed") is False:
            continue

        title = (item.get("title") or "").strip()
        primary_loc = (item.get("location") or "").strip()
        workplace_type_hint = (item.get("workplaceType") or "").strip()
        is_remote = item.get("isRemote") is True

        secondary_locs = [
            (sec.get("location") or "").strip()
            for sec in (item.get("secondaryLocations") or [])
            if isinstance(sec, dict) and sec.get("location")
        ]
        loc_parts = [p for p in [primary_loc, ", ".join(secondary_locs), workplace_type_hint] if p]
        if (
            is_remote
            and workplace_type_hint.lower() not in ("hybrid", "onsite", "on-site")
            and not any("remote" in p.lower() for p in loc_parts)
        ):
            loc_parts.append("Remote")
        location_raw = " | ".join(dict.fromkeys(loc_parts))

        desc_plain = item.get("descriptionPlain") or strip_html(item.get("descriptionHtml") or "")

        passed, workplace_type, job_hash, _ = evaluate_posting(
            company=company_name,
            title=title,
            location_raw=location_raw,
            description_text=desc_plain,
        )
        if not passed:
            continue

        # Parse optional compensation bounds if annual USD salary is provided
        salary_min: Optional[int] = None
        salary_max: Optional[int] = None
        comp = item.get("compensation") or {}
        for summary_comp in (comp.get("summaryComponents") or []):
            if (summary_comp.get("compensationType") == "Salary") and (
                summary_comp.get("interval") in ("1 YEAR", "YEARLY", "Annual", None)
            ):
                low = summary_comp.get("minValue")
                high = summary_comp.get("maxValue")
                if isinstance(low, (int, float)) and low >= 30000:
                    salary_min = int(low)
                if isinstance(high, (int, float)) and high >= 30000:
                    salary_max = int(high)

        matched.append(
            {
                "job_hash": job_hash,
                "source": "ashby",
                "external_id": str(item.get("id")) if item.get("id") is not None else None,
                "company": company_name,
                "title": title,
                "location_raw": location_raw,
                "workplace_type": workplace_type,
                "salary_min": salary_min,
                "salary_max": salary_max,
                "url": item.get("jobUrl") or item.get("applyUrl") or "",
                "description_text": desc_plain,
                "date_posted": _parse_iso_datetime(item.get("publishedAt")),
            }
        )

    return matched, len(raw_jobs)


def fetch_workday_cxs_board(
    tenant: str,
    wd_instance: str,
    site: str,
    company_name: str,
    session: requests.Session,
    search_terms: Sequence[str] = WORKDAY_SEARCH_TERMS,
    page_delay_seconds: float = 0.0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Poll an unauthenticated public Workday CXS JSON endpoint (`/wday/cxs/{tenant}/{site}/jobs`).

    Pre-filters listings by title and location (RTP/NC, DMV Corridor, or US-Remote) before fetching
    individual requisition detail JSONs so network traffic stays fast and polite.
    """
    base_cxs = f"https://{tenant}.{wd_instance}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    public_base = f"https://{tenant}.{wd_instance}.myworkdayjobs.com/en-US/{site}"
    jobs_url = f"{base_cxs}/jobs"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    seen_paths: Dict[str, Dict[str, Any]] = {}
    total_raw_seen = 0

    for idx, term in enumerate(search_terms):
        if idx > 0 and page_delay_seconds > 0:
            time.sleep(page_delay_seconds)
        payload = {"limit": 20, "offset": 0, "searchText": term}
        resp = session.post(jobs_url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json() or {}
        postings = data.get("jobPostings", []) or []
        total_raw_seen += len(postings)
        for item in postings:
            ext_path = (item.get("externalPath") or "").strip()
            if ext_path and ext_path not in seen_paths:
                seen_paths[ext_path] = item

    matched: List[Dict[str, Any]] = []
    for ext_path, item in seen_paths.items():
        title = (item.get("title") or "").strip()
        if is_excluded_title(title) or not is_target_title(title):
            continue

        loc_hint = (item.get("locationsText") or "").strip()
        # If listing already shows a concrete single city/state that fails location filter, skip detail call;
        # if it says "2 Locations" or "Multiple Locations" or passes, inspect the detail payload.
        is_multi_loc = "locations" in loc_hint.lower()
        if loc_hint and not is_multi_loc and classify_location(loc_hint) is None:
            continue

        description_text = ""
        detail_loc = loc_hint
        external_id: Optional[str] = None
        date_posted: Optional[datetime] = None

        bullet_fields = item.get("bulletFields") or []
        if bullet_fields and isinstance(bullet_fields, list):
            external_id = str(bullet_fields[0])[:128]

        try:
            det_resp = session.get(f"{base_cxs}{ext_path}", headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            if getattr(det_resp, "status_code", 0) == 200:
                info = (det_resp.json() or {}).get("jobPostingInfo") or {}
                description_text = strip_html(info.get("jobDescription") or "")
                primary_loc = (info.get("location") or "").strip()
                add_locs = info.get("additionalLocations") or []
                add_locs_str = ", ".join(str(x) for x in add_locs if x) if isinstance(add_locs, list) else ""
                loc_parts = [p for p in [primary_loc, add_locs_str, loc_hint] if p]
                if loc_parts:
                    detail_loc = " | ".join(dict.fromkeys(loc_parts))
                if info.get("jobReqId"):
                    external_id = str(info["jobReqId"])[:128]
                date_posted = _parse_iso_datetime(info.get("startDate"))
        except Exception:
            pass

        passed, workplace_type, job_hash, _ = evaluate_posting(
            company=company_name,
            title=title,
            location_raw=detail_loc,
            description_text=description_text,
        )
        if not passed:
            continue

        matched.append(
            {
                "job_hash": job_hash,
                "source": "workday",
                "external_id": external_id or ext_path[-64:],
                "company": company_name,
                "title": title,
                "location_raw": detail_loc[:255],
                "workplace_type": workplace_type,
                "salary_min": None,
                "salary_max": None,
                "url": f"{public_base}{ext_path}",
                "description_text": description_text,
                "date_posted": date_posted,
            }
        )

    return matched, total_raw_seen


def parse_xml_job_feed(
    xml_text: str,
    company_name: str,
    default_location: str = "Remote - US",
) -> Tuple[List[Dict[str, Any]], int]:
    """Parse a compliant RSS `<item>` or XML Sitemap `<url>` feed and apply knockout filters."""
    if not xml_text or not xml_text.strip():
        return [], 0

    root = ET.fromstring(xml_text.strip())
    matched: List[Dict[str, Any]] = []
    raw_count = 0

    # Strip XML namespace if present
    def _local_tag(elem: ET.Element) -> str:
        return elem.tag.split("}", 1)[-1].lower()

    for elem in root.iter():
        tag = _local_tag(elem)
        if tag not in ("item", "job", "url"):
            continue

        children = {_local_tag(c): (c.text or "").strip() for c in list(elem)}
        title = children.get("title") or ""
        link = children.get("link") or children.get("loc") or ""
        location_raw = children.get("location") or children.get("city") or default_location
        desc_raw = children.get("description") or children.get("content") or ""

        # If a sitemap <url> only has a <loc> URL without a <title>, infer title from URL slug if possible
        if not title and link:
            slug_part = link.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
            title = slug_part

        if not title and not link:
            continue
        raw_count += 1

        description_text = strip_html(desc_raw)
        passed, workplace_type, job_hash, _ = evaluate_posting(
            company=company_name,
            title=title,
            location_raw=location_raw,
            description_text=description_text,
        )
        if not passed:
            continue

        matched.append(
            {
                "job_hash": job_hash,
                "source": "xml_feed",
                "external_id": (children.get("guid") or children.get("reqid") or link[-64:])[:128],
                "company": company_name,
                "title": title,
                "location_raw": location_raw[:255],
                "workplace_type": workplace_type,
                "salary_min": None,
                "salary_max": None,
                "url": link,
                "description_text": description_text,
                "date_posted": None,
            }
        )

    return matched, raw_count


def poll_all_ats(
    config_path: Path = DEFAULT_CONFIG_PATH,
    console: Optional[Console] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Poll all configured Greenhouse and Lever boards and deduplicate by job_hash.

    Returns:
        Tuple of (deduplicated_matched_jobs, board_stats)
    """
    cfg = load_companies_config(config_path)
    deduped: Dict[str, Dict[str, Any]] = {}
    stats: List[Dict[str, Any]] = []

    with requests.Session() as session:
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

        for entry in cfg.get("greenhouse", []):
            slug = entry["slug"]
            name = entry.get("name", slug)
            try:
                matched, raw_count = fetch_greenhouse_board(slug, name, session)
                for job in matched:
                    deduped.setdefault(job["job_hash"], job)
                stats.append(
                    {"ats": "Greenhouse", "company": name, "raw": raw_count, "matched": len(matched), "status": "OK"}
                )
            except Exception as exc:
                stats.append(
                    {"ats": "Greenhouse", "company": name, "raw": 0, "matched": 0, "status": f"ERROR: {exc}"}
                )
                if console:
                    console.print(f"[yellow]Warning:[/yellow] Failed polling Greenhouse ({name}): {exc}")

        for entry in cfg.get("lever", []):
            slug = entry["slug"]
            name = entry.get("name", slug)
            try:
                matched, raw_count = fetch_lever_board(slug, name, session)
                for job in matched:
                    deduped.setdefault(job["job_hash"], job)
                stats.append(
                    {"ats": "Lever", "company": name, "raw": raw_count, "matched": len(matched), "status": "OK"}
                )
            except Exception as exc:
                stats.append(
                    {"ats": "Lever", "company": name, "raw": 0, "matched": 0, "status": f"ERROR: {exc}"}
                )
                if console:
                    console.print(f"[yellow]Warning:[/yellow] Failed polling Lever ({name}): {exc}")

        for entry in cfg.get("ashby", []):
            slug = entry["slug"]
            name = entry.get("name", slug)
            try:
                matched, raw_count = fetch_ashby_board(slug, name, session)
                for job in matched:
                    deduped.setdefault(job["job_hash"], job)
                stats.append(
                    {"ats": "Ashby", "company": name, "raw": raw_count, "matched": len(matched), "status": "OK"}
                )
            except Exception as exc:
                stats.append(
                    {"ats": "Ashby", "company": name, "raw": 0, "matched": 0, "status": f"ERROR: {exc}"}
                )
                if console:
                    console.print(f"[yellow]Warning:[/yellow] Failed polling Ashby ({name}): {exc}")

        for entry in cfg.get("workday", []):
            name = entry.get("name", entry.get("tenant", "Workday"))
            tenant = entry["tenant"]
            wd_instance = entry.get("wd_instance", "wd5")
            site = entry["site"]
            try:
                matched, raw_count = fetch_workday_cxs_board(
                    tenant=tenant,
                    wd_instance=wd_instance,
                    site=site,
                    company_name=name,
                    session=session,
                )
                for job in matched:
                    deduped.setdefault(job["job_hash"], job)
                stats.append(
                    {"ats": "Workday", "company": name, "raw": raw_count, "matched": len(matched), "status": "OK"}
                )
            except Exception as exc:
                stats.append(
                    {"ats": "Workday", "company": name, "raw": 0, "matched": 0, "status": f"ERROR: {exc}"}
                )
                if console:
                    console.print(f"[yellow]Warning:[/yellow] Failed polling Workday ({name}): {exc}")

        for entry in cfg.get("xml_feeds", []):
            name = entry.get("name", "XML Feed")
            feed_url = entry.get("url", "")
            default_loc = entry.get("default_location", "Remote - US")
            if not feed_url:
                continue
            try:
                resp = session.get(feed_url, timeout=REQUEST_TIMEOUT_SECONDS)
                resp.raise_for_status()
                matched, raw_count = parse_xml_job_feed(resp.text, company_name=name, default_location=default_loc)
                for job in matched:
                    deduped.setdefault(job["job_hash"], job)
                stats.append(
                    {"ats": "XML Feed", "company": name, "raw": raw_count, "matched": len(matched), "status": "OK"}
                )
            except Exception as exc:
                stats.append(
                    {"ats": "XML Feed", "company": name, "raw": 0, "matched": 0, "status": f"ERROR: {exc}"}
                )
                if console:
                    console.print(f"[yellow]Warning:[/yellow] Failed polling XML Feed ({name}): {exc}")

    return list(deduped.values()), stats


def main() -> None:
    """CLI entrypoint to verify live ATS job fetching and knockout filtering."""
    console = Console()
    console.print("[bold cyan]Stage 1: Polling Greenhouse & Lever ATS feeds...[/bold cyan]")

    jobs, stats = poll_all_ats(console=console)

    board_table = Table(title="ATS Board Poll Summary", show_lines=False)
    board_table.add_column("ATS", style="cyan")
    board_table.add_column("Company", style="bold")
    board_table.add_column("Total Postings", justify="right")
    board_table.add_column("Passed Filters", justify="right", style="green")
    board_table.add_column("Status")

    total_raw = 0
    for s in stats:
        total_raw += s["raw"]
        board_table.add_row(s["ats"], s["company"], str(s["raw"]), str(s["matched"]), s["status"])

    console.print(board_table)
    console.print(
        f"\n[bold green]Total Raw Postings Scanned:[/bold green] {total_raw} | "
        f"[bold green]Unique Target Matches (Remote US / Local RTP):[/bold green] {len(jobs)}\n"
    )

    if jobs:
        match_table = Table(title="Filtered Target Postings (Preview)", show_lines=True)
        match_table.add_column("Company", style="bold cyan", no_wrap=True)
        match_table.add_column("Title", style="white")
        match_table.add_column("Mode", justify="center")
        match_table.add_column("Location", style="dim")
        match_table.add_column("Hash (prefix)", style="magenta", no_wrap=True)

        for job in jobs[:25]:
            badge = (
                "[green][REMOTE][/green]"
                if job["workplace_type"] == "Remote"
                else f"[yellow][{job['workplace_type'].upper()}][/yellow]"
            )
            match_table.add_row(
                job["company"],
                job["title"],
                badge,
                job["location_raw"] or "",
                job["job_hash"][:10],
            )
        console.print(match_table)


if __name__ == "__main__":
    main()
