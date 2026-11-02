"""Stage 4 Phase 2 Extension: Optional Notion Database Sync for high-scoring job postings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from rich.console import Console
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import JobPosting

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=ENV_PATH)

NOTION_API_URL = "https://api.notion.com/v1/pages"
NOTION_VERSION = "2022-06-28"


def is_notion_configured(api_key: Optional[str] = None, database_id: Optional[str] = None) -> bool:
    """Return True when both NOTION_API_KEY and NOTION_DATABASE_ID are configured."""
    resolved_key = (api_key if api_key is not None else os.getenv("NOTION_API_KEY", "")).strip()
    resolved_db = (database_id if database_id is not None else os.getenv("NOTION_DATABASE_ID", "")).strip()
    return bool(resolved_key and resolved_db)


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
    resolved_db = (database_id if database_id is not None else os.getenv("NOTION_DATABASE_ID", "")).strip()

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
        for row in unsynced:
            payload = {
                "parent": {"database_id": resolved_db},
                "properties": {
                    "Title": {"title": [{"text": {"content": f"{row.company} — {row.title}"}}]},
                    "Company": {"rich_text": [{"text": {"content": row.company}}]},
                    "Match Score": {"number": round(float(row.match_score or 0.0), 2)},
                    "Workplace": {"select": {"name": row.workplace_type}},
                    "URL": {"url": row.url},
                },
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
