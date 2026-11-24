"""Stage 4 Phase 2 Extension: Optional Notion Database Sync for high-scoring job postings."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests
from dotenv import load_dotenv
from rich.console import Console
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from src.db.models import ApplicationEvent, JobPosting
from src.exporters.markdown_digest import format_salary_range
from src.scoring.filters import (
    classify_priority_tier,
    classify_sector,
    format_location_tag,
    format_posting_age,
)

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=ENV_PATH)

NOTION_API_URL = "https://api.notion.com/v1/pages"
NOTION_DATABASE_URL = "https://api.notion.com/v1/databases"
NOTION_VERSION = "2022-06-28"

_UUID_32_RE = re.compile(r"([0-9a-fA-F]{32})")
_UUID_DASHED_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def normalize_notion_database_id(raw_value: Optional[str]) -> str:
    """Extract the 32-hex Notion database ID from either a raw ID or a full Notion share URL."""
    val = (raw_value or "").strip()
    if not val:
        return ""
    # Strip query string first so ?v=... view ID isn't mistaken for the database ID
    base = val.split("?", 1)[0]
    m_dashed = _UUID_DASHED_RE.search(base)
    if m_dashed:
        return m_dashed.group(1).replace("-", "")
    m_32 = _UUID_32_RE.search(base)
    if m_32:
        return m_32.group(1)
    return val


def is_notion_configured(api_key: Optional[str] = None, database_id: Optional[str] = None) -> bool:
    """Return True when both NOTION_API_KEY and NOTION_DATABASE_ID are configured."""
    resolved_key = (api_key if api_key is not None else os.getenv("NOTION_API_KEY", "")).strip()
    resolved_db = normalize_notion_database_id(
        database_id if database_id is not None else os.getenv("NOTION_DATABASE_ID", "")
    )
    return bool(resolved_key and resolved_db)


def _ensure_database_schema(
    client: requests.Session,
    headers: Dict[str, str],
    database_id: str,
) -> tuple[str, str]:
    """Resolve child_database if a parent Page ID was provided, ensure schema columns, and return (db_id, title_prop)."""
    resolved_db_id = database_id
    title_prop_name = "Name"
    try:
        resp = client.get(f"{NOTION_DATABASE_URL}/{resolved_db_id}", headers=headers, timeout=15)
        if not hasattr(resp, "status_code"):
            return resolved_db_id, "Title"

        # If the user pasted a parent Page URL containing an inline database, find the child_database block
        if resp.status_code == 400 and "is a page, not a database" in getattr(resp, "text", ""):
            blocks_resp = client.get(
                f"https://api.notion.com/v1/blocks/{resolved_db_id}/children",
                headers=headers,
                timeout=15,
            )
            if hasattr(blocks_resp, "status_code") and blocks_resp.status_code == 200:
                blocks = (blocks_resp.json() or {}).get("results", []) or []
                for blk in blocks:
                    if isinstance(blk, dict) and blk.get("type") == "child_database" and not blk.get("archived"):
                        resolved_db_id = blk["id"].replace("-", "")
                        break
            resp = client.get(f"{NOTION_DATABASE_URL}/{resolved_db_id}", headers=headers, timeout=15)

        if resp.status_code != 200:
            return resolved_db_id, "Title"

        db_data = resp.json() if callable(getattr(resp, "json", None)) else {}
        if not isinstance(db_data, dict):
            return resolved_db_id, "Title"
        props = db_data.get("properties")
        if not isinstance(props, dict):
            return resolved_db_id, "Title"

        for prop_name, prop_meta in props.items():
            if isinstance(prop_meta, dict) and prop_meta.get("type") == "title":
                title_prop_name = prop_name
                break

        desired_props: Dict[str, Any] = {
            "Company": {"rich_text": {}},
            "Match Score": {"number": {"format": "number"}},
            "Keyword Score": {"number": {"format": "number"}},
            "Semantic Score": {"number": {"format": "number"}},
            "Experience Tier": {"select": {}},
            "Priority": {
                "select": {
                    "options": [
                        {"name": "Tier 1", "color": "green"},
                        {"name": "Tier 2", "color": "yellow"},
                        {"name": "Tier 3", "color": "gray"},
                    ]
                }
            },
            "Sector": {
                "select": {
                    "options": [
                        {"name": "Defense / Federal", "color": "blue"},
                        {"name": "Law Enforcement", "color": "red"},
                        {"name": "Nuclear / Energy", "color": "green"},
                        {"name": "Commodity / Quant", "color": "purple"},
                        {"name": "Tech", "color": "gray"},
                    ]
                }
            },
            "Workplace": {
                "select": {
                    "options": [
                        {"name": "Remote", "color": "blue"},
                        {"name": "Local", "color": "green"},
                    ]
                }
            },
            "Status": {
                "select": {
                    "options": [
                        {"name": "New Match", "color": "blue"},
                        {"name": "Want to Apply", "color": "yellow"},
                        {"name": "Applied", "color": "orange"},
                        {"name": "Follow Up Needed", "color": "purple"},
                        {"name": "Interviewing", "color": "green"},
                        {"name": "Offer", "color": "pink"},
                        {"name": "Declined / Rejected", "color": "red"},
                        {"name": "Not Interested", "color": "gray"},
                    ]
                }
            },
            "Date Applied": {"date": {}},
            "Follow-Up Date": {"date": {}},
            "Contact / Notes": {"rich_text": {}},
            "Salary": {"rich_text": {}},
            "Posted Age": {"rich_text": {}},
            "Source": {"select": {}},
            "Location": {"rich_text": {}},
            "URL": {"url": {}},
        }

        missing_props = {k: v for k, v in desired_props.items() if k not in props}
        if missing_props and hasattr(client, "patch"):
            patch_resp = client.patch(
                f"{NOTION_DATABASE_URL}/{resolved_db_id}",
                headers=headers,
                json={"properties": missing_props},
                timeout=15,
            )
            if hasattr(patch_resp, "raise_for_status"):
                patch_resp.raise_for_status()
    except Exception:
        return resolved_db_id, title_prop_name

    return resolved_db_id, title_prop_name


def sync_to_notion(
    session: Session,
    min_score: float = 65.0,
    api_key: Optional[str] = None,
    database_id: Optional[str] = None,
    console: Optional[Console] = None,
    http_session: Optional[requests.Session] = None,
) -> int:
    """Push unsynced high-scoring rows to Notion and record notion_page_id in MySQL."""
    resolved_key = (api_key if api_key is not None else os.getenv("NOTION_API_KEY", "")).strip()
    resolved_db = normalize_notion_database_id(
        database_id if database_id is not None else os.getenv("NOTION_DATABASE_ID", "")
    )

    if not (resolved_key and resolved_db):
        return 0

    stmt = (
        select(JobPosting)
        .where(
            JobPosting.status == "new",
            JobPosting.match_score.is_not(None),
            JobPosting.match_score >= min_score,
            JobPosting.notion_page_id.is_(None),
        )
        .order_by(JobPosting.match_score.desc())
    )
    unsynced = list(session.scalars(stmt).all())
    if not unsynced:
        return 0

    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

    own_http = http_session is None
    client = http_session or requests.Session()
    synced_count = 0

    try:
        resolved_db, title_prop = _ensure_database_schema(client, headers, resolved_db)

        for row in unsynced:
            mk = row.matched_keywords if isinstance(row.matched_keywords, dict) else {}
            exp_tier = mk.get("experience_tier", "Unspecified (Target)")
            salary_str = format_salary_range(row.salary_min, row.salary_max)

            location_tag = format_location_tag(row.location_raw, row.workplace_type)
            properties: Dict[str, Any] = {
                title_prop: {"title": [{"text": {"content": f"{row.company} — {row.title}"[:200]}}]},
                "Company": {"rich_text": [{"text": {"content": (row.company or "")[:200]}}]},
                "Match Score": {"number": round(float(row.match_score or 0.0), 1)},
                "Workplace": {"select": {"name": location_tag}},
                "URL": {"url": row.url},
            }

            # Populate extended columns when connecting to a live Notion database
            if title_prop != "Title" or own_http:
                properties.update(
                    {
                        "Keyword Score": {"number": round(float(row.keyword_score or 0.0), 1)},
                        "Semantic Score": {"number": round(float(row.semantic_score or 0.0), 1)},
                        "Experience Tier": {"select": {"name": exp_tier}},
                        "Priority": {
                            "select": {
                                "name": classify_priority_tier(
                                    source=row.source,
                                    company=row.company,
                                    title=row.title,
                                    match_score=row.match_score,
                                    date_posted=row.date_posted,
                                    experience_tier=exp_tier,
                                )
                            }
                        },
                        "Sector": {
                            "select": {
                                "name": row.sector
                                or classify_sector(row.company, row.title, row.description_text, row.source)
                            }
                        },
                        "Status": {"select": {"name": "New Match"}},
                        "Salary": {"rich_text": [{"text": {"content": salary_str}}]},
                        "Posted Age": {"rich_text": [{"text": {"content": format_posting_age(row.date_posted)}}]},
                        "Source": {"select": {"name": row.source or "ats"}},
                        "Location": {"rich_text": [{"text": {"content": (row.location_raw or "")[:200]}}]},
                    }
                )

            payload = {
                "parent": {"database_id": resolved_db},
                "properties": properties,
            }
            resp = client.post(NOTION_API_URL, headers=headers, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            page_id = data.get("id")
            if page_id:
                row.notion_page_id = str(page_id)
                synced_count += 1
        session.commit()
        if own_http:
            update_notion_board_counts_banner(
                api_key=resolved_key,
                database_id= database_id if database_id is not None else os.getenv("NOTION_DATABASE_ID", ""),
                http_session=client,
            )
    except Exception as exc:
        if console:
            console.print(f"[yellow]Warning:[/yellow] Notion sync encountered an error: {exc}")
    finally:
        if own_http:
            client.close()

    return synced_count


PRESERVED_NOTION_STATUSES = {
    "Applied",
    "Follow Up Needed",
    "Interviewing",
    "Offer",
    "Declined / Rejected",
}


def archive_expired_notion_pages(
    session: Session,
    expired_rows: Optional[Sequence[JobPosting]] = None,
    api_key: Optional[str] = None,
    console: Optional[Console] = None,
    http_session: Optional[requests.Session] = None,
) -> int:
    """Archive Notion cards for expired postings unless the user already applied to them."""
    resolved_key = (api_key if api_key is not None else os.getenv("NOTION_API_KEY", "")).strip()
    if not resolved_key:
        return 0

    if expired_rows is not None:
        candidates = [r for r in expired_rows if r.notion_page_id]
    else:
        stmt = select(JobPosting).where(
            JobPosting.status == "rejected",
            JobPosting.notion_page_id.is_not(None),
        )
        candidates = list(session.scalars(stmt).all())

    if not candidates:
        return 0

    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

    own_http = http_session is None
    client = http_session or requests.Session()
    archived_count = 0

    try:
        for row in candidates:
            mk = dict(row.matched_keywords) if isinstance(row.matched_keywords, dict) else {}
            if not mk.get("expired_online") or mk.get("notion_archived") or mk.get("notion_preserved"):
                continue

            page_id = row.notion_page_id
            get_resp = client.get(f"{NOTION_API_URL}/{page_id}", headers=headers, timeout=15)
            if hasattr(get_resp, "status_code") and get_resp.status_code == 200:
                page_data = get_resp.json() or {}
                if page_data.get("archived"):
                    mk["notion_archived"] = True
                    row.matched_keywords = mk
                    continue

                props = page_data.get("properties") or {}
                status_prop = props.get("Status") or {}
                select_val = status_prop.get("select") or status_prop.get("status") or {}
                current_status = (select_val.get("name") or "").strip()

                if current_status in PRESERVED_NOTION_STATUSES:
                    # User already applied or tracked an outcome — preserve on Notion board
                    row.status = "applied"
                    mk["notion_preserved"] = True
                    row.matched_keywords = mk
                    continue

            patch_resp = client.patch(
                f"{NOTION_API_URL}/{page_id}",
                headers=headers,
                json={"archived": True},
                timeout=15,
            )
            if hasattr(patch_resp, "raise_for_status"):
                patch_resp.raise_for_status()
            mk["notion_archived"] = True
            row.matched_keywords = mk
            archived_count += 1

        session.commit()
    except Exception as exc:
        if console:
            console.print(f"[yellow]Warning:[/yellow] Notion expired-page cleanup encountered an error: {exc}")
    finally:
        if own_http:
            client.close()

    return archived_count


BOARD_STATUS_ORDER = (
    ("🔵 New Match", "New Match"),
    ("🟡 Want to Apply", "Want to Apply"),
    ("🟠 Applied", "Applied"),
    ("🟣 Follow Up Needed", "Follow Up Needed"),
    ("🟢 Interviewing", "Interviewing"),
    ("🩷 Offer", "Offer"),
    ("🔴 Declined / Rejected", "Declined / Rejected"),
)


def update_notion_board_counts_banner(
    api_key: Optional[str] = None,
    database_id: Optional[str] = None,
    forecast_summary: Optional[str] = None,
    http_session: Optional[requests.Session] = None,
) -> Dict[str, int]:
    """Count cards in each Status column and update the top Callout banner above the Notion board."""
    resolved_key = (api_key if api_key is not None else os.getenv("NOTION_API_KEY", "")).strip()
    raw_id = normalize_notion_database_id(
        database_id if database_id is not None else os.getenv("NOTION_DATABASE_ID", "")
    )
    if not (resolved_key and raw_id):
        return {}

    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }
    own_http = http_session is None
    client = http_session or requests.Session()
    counts: Dict[str, int] = {status_name: 0 for _, status_name in BOARD_STATUS_ORDER}

    try:
        resolved_db, _ = _ensure_database_schema(client, headers, raw_id)
        parent_page_id = raw_id if resolved_db != raw_id else None

        # Paginate through all active cards in the database to tally each Status column
        has_more = True
        next_cursor: Optional[str] = None
        while has_more:
            body: Dict[str, Any] = {"page_size": 100}
            if next_cursor:
                body["start_cursor"] = next_cursor
            q_resp = client.post(
                f"{NOTION_DATABASE_URL}/{resolved_db}/query",
                headers=headers,
                json=body,
                timeout=15,
            )
            if getattr(q_resp, "status_code", 0) != 200:
                break
            q_data = q_resp.json() or {}
            for page in q_data.get("results") or []:
                if not isinstance(page, dict) or page.get("archived"):
                    continue
                props = page.get("properties") or {}
                st_prop = props.get("Status") or {}
                sel = st_prop.get("select") or st_prop.get("status") or {}
                st_name = (sel.get("name") or "").strip()
                if st_name in counts:
                    counts[st_name] += 1
            has_more = bool(q_data.get("has_more"))
            next_cursor = q_data.get("next_cursor")

        if not parent_page_id:
            db_meta = client.get(f"{NOTION_DATABASE_URL}/{resolved_db}", headers=headers, timeout=15)
            if getattr(db_meta, "status_code", 0) == 200:
                parent_info = (db_meta.json() or {}).get("parent") or {}
                if parent_info.get("type") == "page_id":
                    parent_page_id = (parent_info.get("page_id") or "").replace("-", "")

        if parent_page_id:
            blocks_resp = client.get(
                f"https://api.notion.com/v1/blocks/{parent_page_id}/children",
                headers=headers,
                timeout=15,
            )
            if getattr(blocks_resp, "status_code", 0) == 200:
                blocks = (blocks_resp.json() or {}).get("results") or []
                callout_id = None
                for blk in blocks:
                    if isinstance(blk, dict) and blk.get("type") == "callout" and not blk.get("archived"):
                        callout_id = blk.get("id")
                        break

                summary_parts = [f"{label}: {counts[st_name]}" for label, st_name in BOARD_STATUS_ORDER]
                summary_line = "   |   ".join(summary_parts)
                rich_text = [
                    {
                        "type": "text",
                        "text": {"content": "📊 Live Column Counts:   "},
                        "annotations": {"bold": True},
                    },
                    {
                        "type": "text",
                        "text": {"content": f"{summary_line}\n"},
                        "annotations": {"bold": True, "code": True},
                    },
                ]
                if forecast_summary:
                    rich_text.extend(
                        [
                            {
                                "type": "text",
                                "text": {"content": "📈 Offer Forecast:   "},
                                "annotations": {"bold": True},
                            },
                            {
                                "type": "text",
                                "text": {"content": f"{forecast_summary}\n"},
                                "annotations": {"bold": True},
                            },
                        ]
                    )
                rich_text.append(
                    {
                        "type": "text",
                        "text": {
                            "content": (
                                "⚡ Quick Triage: Drag to Want to Apply to queue tailoring  •  "
                                "Drag to Applied (auto-stamps Date Applied + 7d Follow-Up)  •  "
                                "Wrong drop? Check '↩️ Recently Moved' tab or run .\\undo_last_move.ps1"
                            )
                        },
                    }
                )
                if callout_id:
                    client.patch(
                        f"https://api.notion.com/v1/blocks/{callout_id}",
                        headers=headers,
                        json={
                            "callout": {
                                "icon": {"type": "emoji", "emoji": "📊"},
                                "color": "blue_background",
                                "rich_text": rich_text,
                            }
                        },
                        timeout=15,
                    )
    except Exception:
        pass
    finally:
        if own_http:
            client.close()

    return counts


def _parse_notion_iso(ts: Optional[str]) -> datetime:
    if not ts:
        return datetime.utcnow()
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return datetime.utcnow()


def _extract_plain_text(prop: Dict[str, Any]) -> str:
    if not isinstance(prop, dict):
        return ""
    for key in ("title", "rich_text"):
        arr = prop.get(key)
        if isinstance(arr, list):
            return "".join(str(x.get("plain_text") or "") for x in arr if isinstance(x, dict)).strip()
    return ""


TRACKED_TRANSITION_STATUSES = {
    "Want to Apply",
    "Applied",
    "Follow Up Needed",
    "Interviewing",
    "Offer",
    "Declined / Rejected",
    "Not Interested",
}


def sync_notion_board_transitions(
    session: Session,
    api_key: Optional[str] = None,
    database_id: Optional[str] = None,
    auto_stamp_dates: bool = True,
    console: Optional[Console] = None,
    http_session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Poll Notion board state, log any column changes into `application_events`, and auto-stamp Applied dates."""
    resolved_key = (api_key if api_key is not None else os.getenv("NOTION_API_KEY", "")).strip()
    raw_id = normalize_notion_database_id(
        database_id if database_id is not None else os.getenv("NOTION_DATABASE_ID", "")
    )
    if not (resolved_key and raw_id):
        return {"scanned": 0, "events_logged": 0, "dates_stamped": 0}

    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }
    own_http = http_session is None
    client = http_session or requests.Session()
    scanned = 0
    events_logged = 0
    dates_stamped = 0

    try:
        resolved_db, title_prop = _ensure_database_schema(client, headers, raw_id)
        has_more = True
        next_cursor: Optional[str] = None

        while has_more:
            body: Dict[str, Any] = {"page_size": 100}
            if next_cursor:
                body["start_cursor"] = next_cursor
            q_resp = client.post(
                f"{NOTION_DATABASE_URL}/{resolved_db}/query",
                headers=headers,
                json=body,
                timeout=20,
            )
            if getattr(q_resp, "status_code", 0) != 200:
                break
            q_data = q_resp.json() or {}
            pages = q_data.get("results") or []

            for page in pages:
                if not isinstance(page, dict) or page.get("archived"):
                    continue
                scanned += 1
                page_id = str(page.get("id") or "")
                page_id_nodash = page_id.replace("-", "")
                props = page.get("properties") or {}

                st_sel = (props.get("Status") or {}).get("select") or (props.get("Status") or {}).get("status") or {}
                current_status = (st_sel.get("name") or "New Match").strip()
                prio_sel = (props.get("Priority") or {}).get("select") or {}
                priority_tier = (prio_sel.get("name") or "").strip() or None
                sec_sel = (props.get("Sector") or {}).get("select") or {}
                sector_val = (sec_sel.get("name") or "").strip() or None
                url_prop = str((props.get("URL") or {}).get("url") or "").strip()
                raw_title = _extract_plain_text(props.get(title_prop) or props.get("Name") or {})
                company_prop = _extract_plain_text(props.get("Company") or {})
                score_prop = (props.get("Match Score") or {}).get("number")
                date_applied_str = ((props.get("Date Applied") or {}).get("date") or {}).get("start")
                followup_str = ((props.get("Follow-Up Date") or {}).get("date") or {}).get("start")
                last_edited_dt = _parse_notion_iso(page.get("last_edited_time"))

                # Correlate with MySQL JobPosting
                conds = []
                if page_id:
                    conds.append(JobPosting.notion_page_id == page_id)
                if page_id_nodash and page_id_nodash != page_id:
                    conds.append(JobPosting.notion_page_id == page_id_nodash)
                if url_prop:
                    conds.append(JobPosting.url == url_prop)

                row: Optional[JobPosting] = None
                if conds:
                    row = session.scalars(select(JobPosting).where(or_(*conds)).limit(1)).first()

                # Determine previous status from JobPosting or latest ApplicationEvent
                prev_status: Optional[str] = None
                if row is not None:
                    prev_status = row.notion_status
                    if not sector_val:
                        sector_val = row.sector or classify_sector(row.company, row.title, row.description_text, row.source)
                        row.sector = sector_val
                else:
                    last_evt = session.scalars(
                        select(ApplicationEvent)
                        .where(ApplicationEvent.notion_page_id == page_id)
                        .order_by(desc(ApplicationEvent.id))
                        .limit(1)
                    ).first()
                    if last_evt is not None:
                        prev_status = last_evt.to_status

                # Auto-stamp Date Applied & +7d Follow-Up Date on Notion when moved to Applied without dates
                if auto_stamp_dates and current_status == "Applied" and not date_applied_str:
                    applied_day = last_edited_dt.date()
                    followup_day = applied_day + timedelta(days=7)
                    patch_props: Dict[str, Any] = {
                        "Date Applied": {"date": {"start": applied_day.isoformat()}},
                    }
                    if not followup_str:
                        patch_props["Follow-Up Date"] = {"date": {"start": followup_day.isoformat()}}
                    p_resp = client.patch(
                        f"{NOTION_API_URL}/{page_id}",
                        headers=headers,
                        json={"properties": patch_props},
                        timeout=15,
                    )
                    if getattr(p_resp, "status_code", 0) == 200:
                        dates_stamped += 1
                        date_applied_str = applied_day.isoformat()

                # Log transition if status changed (or first capture of an active non-'New Match' status)
                status_changed = (prev_status is not None and prev_status != current_status) or (
                    prev_status is None and current_status in TRACKED_TRANSITION_STATUSES
                )
                if status_changed:
                    event_dt = (
                        _parse_notion_iso(date_applied_str)
                        if (current_status == "Applied" and date_applied_str)
                        else last_edited_dt
                    )
                    comp_name = (row.company if row else company_prop) or (
                        raw_title.split(" — ", 1)[0].strip() if " — " in raw_title else "Unknown Company"
                    )
                    title_name = (row.title if row else raw_title) or "Target Role"
                    session.add(
                        ApplicationEvent(
                            job_hash=row.job_hash if row else None,
                            notion_page_id=page_id,
                            company=comp_name[:128],
                            title=title_name[:255],
                            from_status=prev_status,
                            to_status=current_status,
                            event_at=event_dt,
                            priority_tier=priority_tier,
                            sector=sector_val,
                            source=row.source if row else "notion",
                            match_score=float(row.match_score if (row and row.match_score is not None) else (score_prop or 0.0)),
                        )
                    )
                    events_logged += 1

                if row is not None:
                    row.notion_status = current_status
                    if date_applied_str and row.date_applied is None:
                        row.date_applied = _parse_notion_iso(date_applied_str)
                    elif current_status == "Applied" and row.date_applied is None:
                        row.date_applied = last_edited_dt
                    if current_status in {"Interviewing", "Offer", "Declined / Rejected"} and row.date_outcome is None:
                        row.date_outcome = last_edited_dt
                    if current_status in PRESERVED_NOTION_STATUSES:
                        row.status = "applied"

            has_more = bool(q_data.get("has_more"))
            next_cursor = q_data.get("next_cursor")

        session.commit()
    except Exception as exc:
        if console:
            console.print(f"[yellow]Warning:[/yellow] Notion transition sync error: {exc}")
    finally:
        if own_http:
            client.close()

    return {"scanned": scanned, "events_logged": events_logged, "dates_stamped": dates_stamped}


def undo_last_notion_transition(
    session: Session,
    steps: int = 1,
    dry_run: bool = False,
    api_key: Optional[str] = None,
    console: Optional[Console] = None,
    http_session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
    """Revert the last N logged Notion board card moves back to their previous Status and clean application_events."""
    resolved_key = (api_key if api_key is not None else os.getenv("NOTION_API_KEY", "")).strip()
    if not resolved_key:
        return []

    events = list(
        session.scalars(
            select(ApplicationEvent)
            .where(ApplicationEvent.from_status.is_not(None))
            .order_by(desc(ApplicationEvent.id))
            .limit(max(1, steps))
        ).all()
    )
    if not events:
        if console:
            console.print("[yellow]No revertible board transitions found in application_events.[/yellow]")
        return []

    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }
    own_http = http_session is None
    client = http_session or requests.Session()
    reverted: List[Dict[str, Any]] = []

    try:
        for evt in events:
            target_status = evt.from_status or "New Match"
            record = {
                "event_id": evt.id,
                "notion_page_id": evt.notion_page_id,
                "company": evt.company,
                "title": evt.title,
                "reverted_from": evt.to_status,
                "reverted_to": target_status,
                "dry_run": dry_run,
            }
            if not dry_run:
                resp = client.patch(
                    f"{NOTION_API_URL}/{evt.notion_page_id}",
                    headers=headers,
                    json={"properties": {"Status": {"select": {"name": target_status}}}},
                    timeout=15,
                )
                if hasattr(resp, "raise_for_status"):
                    resp.raise_for_status()

                if evt.job_hash:
                    row = session.get(JobPosting, evt.job_hash)
                    if row is not None:
                        row.notion_status = target_status
                        if evt.to_status == "Applied" and target_status != "Applied":
                            row.date_applied = None
                        if evt.to_status in {"Interviewing", "Offer", "Declined / Rejected"}:
                            row.date_outcome = None
                session.delete(evt)
            reverted.append(record)

        if not dry_run:
            session.commit()
    finally:
        if own_http:
            client.close()

    return reverted


if __name__ == "__main__":
    import argparse
    from src.db.session import get_session_factory, init_db

    parser = argparse.ArgumentParser(description="Notion sync & undo utilities.")
    parser.add_argument("--undo", action="store_true", help="Undo the most recent logged Notion board card move.")
    parser.add_argument("--steps", type=int, default=1, help="Number of recent card moves to undo (default: 1).")
    parser.add_argument("--dry-run", action="store_true", help="Preview undo without modifying Notion or MySQL.")
    cli_args = parser.parse_args()

    out = Console()
    eng = init_db()
    SessionLocal = get_session_factory(eng)
    with SessionLocal() as db_sess:
        # First sync any unlogged moves from Notion before undoing
        sync_res = sync_notion_board_transitions(db_sess, auto_stamp_dates=not cli_args.undo, console=out)
        if cli_args.undo:
            undone = undo_last_notion_transition(db_sess, steps=cli_args.steps, dry_run=cli_args.dry_run, console=out)
            for item in undone:
                prefix = "[DRY RUN] Would revert" if cli_args.dry_run else "Reverted"
                out.print(
                    f"[bold green]{prefix}:[/bold green] {item['company']} — {item['title']} "
                    f"([yellow]{item['reverted_from']}[/yellow] -> [cyan]{item['reverted_to']}[/cyan])"
                )
            if undone and not cli_args.dry_run:
                update_notion_board_counts_banner()
        else:
            out.print(f"Transition Sync Complete: {sync_res}")
