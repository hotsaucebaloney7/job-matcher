"""Stage 4 Phase 2 Extension: Optional Notion Database Sync for high-scoring job postings."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv
from rich.console import Console
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import JobPosting
from src.exporters.markdown_digest import format_salary_range

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

            properties: Dict[str, Any] = {
                title_prop: {"title": [{"text": {"content": f"{row.company} — {row.title}"[:200]}}]},
                "Company": {"rich_text": [{"text": {"content": (row.company or "")[:200]}}]},
                "Match Score": {"number": round(float(row.match_score or 0.0), 1)},
                "Workplace": {"select": {"name": row.workplace_type or "Remote"}},
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
    except Exception as exc:
        if console:
            console.print(f"[yellow]Warning:[/yellow] Notion sync encountered an error: {exc}")
    finally:
        if own_http:
            client.close()

    return synced_count
