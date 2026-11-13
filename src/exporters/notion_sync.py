"""Stage 4 Phase 2 Extension: Optional Notion Database Sync for high-scoring job postings."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests
from dotenv import load_dotenv
from rich.console import Console
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import JobPosting
from src.exporters.markdown_digest import format_salary_range
from src.scoring.filters import format_location_tag

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
                        "Status": {"select": {"name": "New Match"}},
                        "Salary": {"rich_text": [{"text": {"content": salary_str}}]},
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
                    {
                        "type": "text",
                        "text": {
                            "content": (
                                "⚡ Quick Triage: Set Status = Want to Apply to queue for resume tailoring  •  "
                                "Set Status = Applied (+ Date Applied & Follow-Up Date) when submitted  •  "
                                "Set Status = Not Interested to hide from board."
                            )
                        },
                    },
                ]
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
