"""Direct ATS Poller for Greenhouse and Lever public JSON job boards."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml
from rich.console import Console
from rich.table import Table

from src.scoring.filters import evaluate_posting, strip_html

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "companies.yaml"
REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = "JobMatcher/1.0 (+local-personal-job-search)"


def load_companies_config(config_path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, List[Dict[str, str]]]:
    """Load Greenhouse and Lever company board definitions from YAML."""
    if not config_path.exists():
        raise FileNotFoundError(f"Companies configuration file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {
        "greenhouse": data.get("greenhouse", []) or [],
        "lever": data.get("lever", []) or [],
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
                "location_raw": primary_loc or location_raw,
                "workplace_type": workplace_type,
                "salary_min": int(salary_min) if isinstance(salary_min, (int, float)) else None,
                "salary_max": int(salary_max) if isinstance(salary_max, (int, float)) else None,
                "url": item.get("hostedUrl") or item.get("applyUrl") or "",
                "description_text": full_description,
                "date_posted": _parse_epoch_ms(item.get("createdAt")),
            }
        )

    return matched, len(raw_jobs)


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
