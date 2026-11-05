"""Stage 4 Extension: Automated 'Want to Apply' Notion Queue Processor & Gemini 4 Argon Resume Tailor.

Scans the Notion 'Job Applications & Matches' database for cards marked `Status = "Want to Apply"`,
correlates them with the full job descriptions and matched/missing keyword breakdowns in MySQL,
and generates a high-precision Gemini 4 Argon tailoring prompt packet (plus optional direct
`GEMINI_API_KEY` autopilot output). Writes the prompt/tailored bullets into each Notion card's page
body and saves a local Markdown packet in `data/tailored/` ready to copy-paste (`Ctrl+V`) into Argon.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests
from dotenv import load_dotenv
from rich.console import Console
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.db.models import JobPosting
from src.db.session import get_session_factory, init_db
from src.exporters.notion_sync import (
    NOTION_DATABASE_URL,
    NOTION_VERSION,
    _ensure_database_schema,
    normalize_notion_database_id,
)
from src.scoring.embed_scorer import DEFAULT_DATA_DIR, load_resume_text

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=ENV_PATH)

DEFAULT_TAILORED_DIR = DEFAULT_DATA_DIR / "tailored"
NOTION_BLOCKS_URL = "https://api.notion.com/v1/blocks"
NOTION_FILE_UPLOADS_URL = "https://api.notion.com/v1/file_uploads"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _extract_notion_text(prop: Dict[str, Any]) -> str:
    """Extract plain text from a Notion title or rich_text property object."""
    if not isinstance(prop, dict):
        return ""
    ptype = prop.get("type")
    items = prop.get(ptype or "", []) if ptype in ("title", "rich_text") else (
        prop.get("title") or prop.get("rich_text") or []
    )
    if not isinstance(items, list):
        return ""
    parts: List[str] = []
    for item in items:
        if isinstance(item, dict):
            plain = item.get("plain_text") or (item.get("text") or {}).get("content") or ""
            if plain:
                parts.append(str(plain))
    return "".join(parts).strip()


def fetch_want_to_apply_jobs(
    session: Session,
    api_key: Optional[str] = None,
    database_id: Optional[str] = None,
    target_status: str = "Want to Apply",
    http_session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
    """Query Notion for all cards with `Status == target_status` and enrich with MySQL job details."""
    resolved_key = (api_key if api_key is not None else os.getenv("NOTION_API_KEY", "")).strip()
    resolved_db = normalize_notion_database_id(
        database_id if database_id is not None else os.getenv("NOTION_DATABASE_ID", "")
    )
    if not (resolved_key and resolved_db):
        return []

    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

    own_http = http_session is None
    client = http_session or requests.Session()
    results: List[Dict[str, Any]] = []

    try:
        resolved_db, title_prop = _ensure_database_schema(client, headers, resolved_db)
        query_payload = {
            "filter": {
                "property": "Status",
                "select": {"equals": target_status},
            },
            "sorts": [
                {"property": "Match Score", "direction": "descending"},
            ],
        }
        resp = client.post(
            f"{NOTION_DATABASE_URL}/{resolved_db}/query",
            headers=headers,
            json=query_payload,
            timeout=20,
        )
        resp.raise_for_status()
        pages = (resp.json() or {}).get("results", []) or []

        for page in pages:
            if not isinstance(page, dict) or page.get("archived"):
                continue

            page_id = str(page.get("id") or "")
            page_id_nodash = page_id.replace("-", "")
            props = page.get("properties") or {}

            raw_title = _extract_notion_text(props.get(title_prop) or props.get("Name") or props.get("Title") or {})
            company_prop = _extract_notion_text(props.get("Company") or {})
            url_prop = str((props.get("URL") or {}).get("url") or "").strip()
            score_prop = (props.get("Match Score") or {}).get("number")
            workplace_prop = ((props.get("Workplace") or {}).get("select") or {}).get("name") or "Remote"

            # Correlate with MySQL JobPosting row via notion_page_id (dashed or undashed) or URL
            conditions = []
            if page_id:
                conditions.append(JobPosting.notion_page_id == page_id)
            if page_id_nodash and page_id_nodash != page_id:
                conditions.append(JobPosting.notion_page_id == page_id_nodash)
            if url_prop:
                conditions.append(JobPosting.url == url_prop)

            db_row: Optional[JobPosting] = None
            if conditions:
                stmt = select(JobPosting).where(or_(*conditions)).limit(1)
                db_row = session.scalars(stmt).first()

            if db_row is not None:
                mk = db_row.matched_keywords if isinstance(db_row.matched_keywords, dict) else {}
                matched_groups = mk.get("matched_groups") if isinstance(mk.get("matched_groups"), dict) else {}
                missing_groups = mk.get("missing_groups") if isinstance(mk.get("missing_groups"), list) else []
                exp_tier = str(mk.get("experience_tier") or "Unspecified (Target)")

                results.append(
                    {
                        "notion_page_id": page_id,
                        "job_hash": db_row.job_hash,
                        "company": db_row.company or company_prop or "Unknown Company",
                        "title": db_row.title or raw_title or "Target Role",
                        "workplace_type": db_row.workplace_type or workplace_prop,
                        "location_raw": db_row.location_raw or "",
                        "url": db_row.url or url_prop,
                        "match_score": float(db_row.match_score or score_prop or 0.0),
                        "keyword_score": float(db_row.keyword_score or 0.0),
                        "semantic_score": float(db_row.semantic_score or 0.0),
                        "experience_tier": exp_tier,
                        "matched_groups": matched_groups,
                        "missing_groups": missing_groups,
                        "description_text": (db_row.description_text or "").strip(),
                    }
                )
            else:
                # Fallback if card was manually added in Notion without a MySQL row
                company = company_prop
                title = raw_title
                if " — " in raw_title and not company:
                    parts = raw_title.split(" — ", 1)
                    company, title = parts[0].strip(), parts[1].strip()
                results.append(
                    {
                        "notion_page_id": page_id,
                        "job_hash": None,
                        "company": company or "Unknown Company",
                        "title": title or "Target Role",
                        "workplace_type": workplace_prop,
                        "location_raw": _extract_notion_text(props.get("Location") or {}),
                        "url": url_prop,
                        "match_score": float(score_prop or 0.0),
                        "keyword_score": 0.0,
                        "semantic_score": 0.0,
                        "experience_tier": "Unspecified (Target)",
                        "matched_groups": {},
                        "missing_groups": [],
                        "description_text": "",
                    }
                )
    finally:
        if own_http:
            client.close()

    return results


def build_argon_tailoring_prompt(
    jobs: Sequence[Dict[str, Any]],
    resume_text: str,
    resume_filename: str = "Josh_Engelhardt_Resume-v3.docx",
) -> str:
    """Build a structured Gemini 4 Argon resume tailoring prompt for one or more 'Want to Apply' jobs."""
    lines: List[str] = [
        "# 🎯 Gemini 4 Argon — Automated Resume Tailoring Packet",
        "",
        "You are an expert technical executive resume writer and ATS optimization specialist for **Data Science, Machine Learning, and AI Engineering** roles.",
        "Below is my current master resume followed by the target job posting(s) from my Notion **`Want to Apply`** queue.",
        "",
        "## 🛠️ Tailoring Instructions for Each Job",
        "For **each** target job below, provide:",
        "1. **Tailored Professional Summary (3–4 lines):** Mirror the exact role title, domain focus, and top matched/missing ATS keywords while staying 100% truthful to my background.",
        "2. **High-Impact Experience & Project Bullet Rewrites (4–6 bullets):** Select the strongest bullets from my master resume and rewrite them using the `Action Verb + Technical Method/Tool + Quantified Impact` formula to align with the job's core responsibilities.",
        "3. **ATS Skills & Keywords Alignment:** Explicitly list which **Missing Skill Groups** can be naturally woven into my Technical Skills section or project descriptions based on my adjacent experience (never fabricate degrees, employers, or years of experience).",
        "4. **3-Sentence Cover Letter / Cold Outreach Hook:** A concise, high-signal opening paragraph I can paste into the application portal or send to the hiring manager.",
        "",
        "---",
        "",
        f"## 📄 Candidate Master Resume (`{resume_filename}`)",
        "```text",
        resume_text.strip(),
        "```",
        "",
        "---",
        "",
        f"## 🎯 Target Jobs in `'Want to Apply'` Queue ({len(jobs)} total)",
        "",
    ]

    for idx, job in enumerate(jobs, start=1):
        company = job.get("company") or "Unknown Company"
        title = job.get("title") or "Target Role"
        workplace = job.get("workplace_type") or "Remote"
        location = job.get("location_raw") or workplace
        url = job.get("url") or ""
        match_score = float(job.get("match_score") or 0.0)
        kw_score = float(job.get("keyword_score") or 0.0)
        sem_score = float(job.get("semantic_score") or 0.0)
        exp_tier = job.get("experience_tier") or "Unspecified (Target)"

        matched_groups: Dict[str, List[str]] = job.get("matched_groups") or {}
        missing_groups: List[str] = job.get("missing_groups") or []

        matched_str = (
            ", ".join(f"{grp} ({'/'.join(terms[:3])})" for grp, terms in matched_groups.items())
            if matched_groups
            else "See job description below"
        )
        missing_str = ", ".join(missing_groups) if missing_groups else "None flagged"
        desc = (job.get("description_text") or "").strip()

        lines.extend(
            [
                f"### Job #{idx}: {company} — {title} (`[{workplace.upper()}]`)",
                f"- **Location:** {location}",
                f"- **Posting URL:** {url}",
                f"- **Hybrid Match Score:** `{match_score:.1f}/100` (Keyword: `{kw_score:.1f}`, Semantic BGE: `{sem_score:.1f}`, Tier: `{exp_tier}`)",
                f"- **✅ Matched Skill Groups Already Detected:** {matched_str}",
                f"- **🎯 Missing / High-Value ATS Skill Groups to Weave In (if applicable):** {missing_str}",
                "",
                "#### Full Job Description:",
                "```text",
                desc if desc else "(No cached description text found in database — tailor based on role title and company domain.)",
                "```",
                "",
                "---",
                "",
            ]
        )

    return "\n".join(lines).strip() + "\n"


def tailor_with_gemini_api(
    prompt: str,
    gemini_api_key: Optional[str] = None,
    model_name: str = "gemini-2.5-flash",
    http_session: Optional[requests.Session] = None,
) -> Optional[str]:
    """Optional autopilot: if GEMINI_API_KEY is configured, generate tailored resume sections directly."""
    resolved_key = (gemini_api_key if gemini_api_key is not None else os.getenv("GEMINI_API_KEY", "")).strip()
    if not resolved_key:
        return None

    own_http = http_session is None
    client = http_session or requests.Session()
    url = f"{GEMINI_API_BASE}/{model_name}:generateContent?key={resolved_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.35},
    }
    try:
        resp = client.post(url, json=payload, timeout=90)
        resp.raise_for_status()
        data = resp.json() or {}
        candidates = data.get("candidates") or []
        if candidates:
            parts = ((candidates[0].get("content") or {}).get("parts")) or []
            texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("text")]
            if texts:
                return "\n".join(texts).strip()
    except Exception:
        return None
    finally:
        if own_http:
            client.close()
    return None


def _chunk_text_for_notion(text: str, max_len: int = 1900) -> List[str]:
    """Split text into <=1900-char chunks to respect Notion's 2000-char rich_text block limit."""
    cleaned = (text or "").strip()
    if not cleaned:
        return [""]
    chunks: List[str] = []
    start = 0
    while start < len(cleaned):
        end = min(start + max_len, len(cleaned))
        if end < len(cleaned):
            nl = cleaned.rfind("\n", start, end)
            if nl > start + 200:
                end = nl + 1
        chunks.append(cleaned[start:end])
        start = end
    return chunks


def _upload_txt_to_notion(
    client: requests.Session,
    api_key: str,
    filename: str,
    content_text: str,
) -> Optional[str]:
    """Upload a plain-text prompt file via Notion's File Uploads API and return the file_upload ID."""
    json_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }
    multipart_headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
    }
    try:
        init_resp = client.post(
            NOTION_FILE_UPLOADS_URL,
            headers=json_headers,
            json={"filename": filename, "content_type": "text/plain"},
            timeout=15,
        )
        if getattr(init_resp, "status_code", 0) != 200:
            return None
        init_data = init_resp.json() if callable(getattr(init_resp, "json", None)) else {}
        if not isinstance(init_data, dict):
            return None
        upload_id = init_data.get("id")
        if not upload_id or not isinstance(upload_id, str):
            return None

        send_resp = client.post(
            f"{NOTION_FILE_UPLOADS_URL}/{upload_id}/send",
            headers=multipart_headers,
            files={"file": (filename, content_text.encode("utf-8"), "text/plain")},
            timeout=20,
        )
        if getattr(send_resp, "status_code", 0) != 200:
            return None
        return upload_id
    except Exception:
        return None


def write_tailoring_to_notion_page(
    page_id: str,
    job: Dict[str, Any],
    single_job_prompt: str,
    prompt_file_path: Optional[Path] = None,
    tailored_output: Optional[str] = None,
    api_key: Optional[str] = None,
    http_session: Optional[requests.Session] = None,
) -> bool:
    """Append an Argon Tailoring Kit (with clickable .txt attachment + full prompt) inside the Notion card's page body."""
    resolved_key = (api_key if api_key is not None else os.getenv("NOTION_API_KEY", "")).strip()
    if not (resolved_key and page_id):
        return False

    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

    own_http = http_session is None
    client = http_session or requests.Session()

    matched_groups: Dict[str, List[str]] = job.get("matched_groups") or {}
    missing_groups: List[str] = job.get("missing_groups") or []
    matched_summary = ", ".join(matched_groups.keys()) if matched_groups else "None detected"
    missing_summary = ", ".join(missing_groups) if missing_groups else "None flagged"
    resolved_file_str = str(prompt_file_path.resolve()) if prompt_file_path is not None else str(DEFAULT_TAILORED_DIR.resolve())
    folder_str = str((prompt_file_path.parent if prompt_file_path is not None else DEFAULT_TAILORED_DIR).resolve())
    filename_only = prompt_file_path.name if prompt_file_path is not None else "Argon_Prompt.txt"

    try:
        existing_resp = client.get(
            f"{NOTION_BLOCKS_URL}/{page_id}/children",
            headers=headers,
            timeout=15,
        )
        if hasattr(existing_resp, "status_code") and existing_resp.status_code == 200:
            existing_blocks = (existing_resp.json() or {}).get("results") or []
            if isinstance(existing_blocks, list):
                for blk in existing_blocks:
                    if isinstance(blk, dict) and blk.get("type") == "file":
                        return True
                    # Clean up previous blocks so we replace cleanly with the clickable .txt attachment version
                    if isinstance(blk, dict) and blk.get("type") in (
                        "divider",
                        "heading_2",
                        "callout",
                        "code",
                        "toggle",
                        "file",
                    ):
                        blk_id = blk.get("id")
                        if blk_id and hasattr(client, "delete"):
                            client.delete(f"{NOTION_BLOCKS_URL}/{blk_id}", headers=headers, timeout=15)

        children: List[Dict[str, Any]] = [
            {"object": "block", "type": "divider", "divider": {}},
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": "🎯 Argon Resume Tailoring Kit"}}]
                },
            },
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "icon": {"type": "emoji", "emoji": "📂"},
                    "color": "blue_background",
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {
                                "content": (
                                    f"✅ Matched Skills: {matched_summary}\n"
                                    f"🎯 Missing ATS Keywords to Weave In: {missing_summary}\n"
                                    f"📁 Local Copy: {resolved_file_str}\n"
                                    f"👇 Click the .txt file below to open directly in your default text editor!"
                                )[:1950]
                            },
                        }
                    ],
                },
            },
        ]

        # Upload the .txt file directly to Notion so the user gets a native clickable .txt attachment block!
        upload_id = _upload_txt_to_notion(
            client=client,
            api_key=resolved_key,
            filename=filename_only,
            content_text=single_job_prompt,
        )
        if upload_id:
            children.append(
                {
                    "object": "block",
                    "type": "file",
                    "file": {
                        "type": "file_upload",
                        "file_upload": {"id": upload_id},
                        "caption": [
                            {
                                "type": "text",
                                "text": {"content": f"Click to open {filename_only} in your default .txt editor"},
                            }
                        ],
                    },
                }
            )

        if tailored_output:
            children.append(
                {
                    "object": "block",
                    "type": "heading_3",
                    "heading_3": {
                        "rich_text": [{"type": "text", "text": {"content": "🤖 Tailored Resume Output"}}]
                    },
                }
            )
            for chunk in _chunk_text_for_notion(tailored_output):
                children.append(
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [{"type": "text", "text": {"content": chunk}}]
                        },
                    }
                )

        # Also keep the full untruncated Argon prompt inside a collapsible toggle block
        prompt_chunks = _chunk_text_for_notion(single_job_prompt)
        unified_code_block = {
            "object": "block",
            "type": "code",
            "code": {
                "language": "plain text",
                "rich_text": [
                    {"type": "text", "text": {"content": chunk}}
                    for chunk in prompt_chunks[:95]
                ],
            },
        }
        children.append(
            {
                "object": "block",
                "type": "toggle",
                "toggle": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": "📋 Full Untruncated Gemini 4 Argon Prompt (Click to expand, then click Copy once)"},
                        }
                    ],
                    "children": [unified_code_block],
                },
            }
        )

        resp = client.patch(
            f"{NOTION_BLOCKS_URL}/{page_id}/children",
            headers=headers,
            json={"children": children[:95]},
            timeout=20,
        )
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        return True
    except Exception:
        return False
    finally:
        if own_http:
            client.close()


def _slugify(value: str) -> str:
    """Create a filesystem-safe slug for tailored output filenames."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", (value or "").strip())
    return cleaned.strip("_")[:40] or "Job"


_STOP_WORDS = {"the", "inc", "llc", "corp", "corporation", "company", "group", "services", "financial"}


def _meaningful_tokens(text: str) -> List[str]:
    """Extract lowercase alphanumeric tokens excluding corporate stopwords."""
    raw = [t.lower() for t in re.findall(r"[a-zA-Z0-9]+", text or "")]
    return [t for t in raw if t not in _STOP_WORDS and len(t) >= 2]


def find_existing_tailored_resume(
    company: str,
    title: str,
    output_dir: Path = DEFAULT_TAILORED_DIR,
) -> Optional[Path]:
    """Return an existing tailored resume file (.docx, .pdf) in `data/tailored/` if one was already generated."""
    if not output_dir.exists():
        return None

    company_slug = _slugify(company).lower()
    title_slug = _slugify(title).lower()
    full_slug = f"{company_slug}_{title_slug}"

    company_tokens = _meaningful_tokens(company)
    primary_company_token = company_tokens[0] if company_tokens else company_slug
    title_tokens = [
        t for t in _meaningful_tokens(title)
        if t not in {"senior", "jr", "junior", "ii", "iii", "iv", "staff", "lead"}
    ]

    best_match: Optional[Path] = None
    best_score = 0

    for p in output_dir.iterdir():
        if not p.is_file():
            continue
        name_low = p.name.lower()
        # Ignore raw Argon prompt input files
        if "argon_prompt" in name_low or "tailoring_prompt" in name_low:
            continue
        if p.suffix.lower() not in {".docx", ".pdf", ".md", ".txt"}:
            continue

        # Exact full slug match
        if full_slug and full_slug in name_low:
            return p

        # Check if primary company token is in the filename
        has_company = (
            (company_slug and len(company_slug) >= 3 and company_slug in name_low)
            or (primary_company_token and len(primary_company_token) >= 3 and primary_company_token in name_low)
        )
        if not has_company:
            continue

        # Count how many distinctive title tokens appear in the filename
        file_tokens = set(_meaningful_tokens(p.stem))
        overlap = sum(1 for t in title_tokens if t in file_tokens or t in name_low)
        # Distinguish e.g. "Reddit Ads Integrity" (2 overlap) vs "Reddit Ads" (1 overlap: 'ads', not 'integrity')
        non_matching_file_tokens = file_tokens - set(company_tokens) - {"josh", "engelhardt", "resume", "tailored", "ds", "ml", "ai"}
        extra_penalty = len(non_matching_file_tokens - set(title_tokens))

        score = overlap * 10 - extra_penalty
        if overlap >= 1 and score > best_score:
            best_score = score
            best_match = p
        elif best_match is None and ("resume" in name_low or "tailored" in name_low) and len(company_tokens) == 1 and company_tokens[0] != "reddit":
            # Single-job company match (e.g. Ampcus -> Josh_Engelhardt_Resume_Ampcus_Applied_AI.docx)
            best_match = p

    return best_match


def process_want_to_apply_queue(
    session: Session,
    resume_path: Optional[Path] = None,
    output_dir: Path = DEFAULT_TAILORED_DIR,
    api_key: Optional[str] = None,
    database_id: Optional[str] = None,
    gemini_api_key: Optional[str] = None,
    update_notion_pages: bool = True,
    force_regenerate: bool = False,
    console: Optional[Console] = None,
    http_session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """End-to-end processor for all Notion cards with `Status = "Want to Apply"`, skipping already-tailored jobs."""
    resume_text, resolved_resume_path = load_resume_text(explicit_path=resume_path)
    jobs = fetch_want_to_apply_jobs(
        session=session,
        api_key=api_key,
        database_id=database_id,
        http_session=http_session,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    today_str = date.today().isoformat()
    batch_prompt_path = output_dir / f"argon_tailoring_prompt_{today_str}.txt"

    if not jobs:
        return {
            "jobs_found": 0,
            "pending_jobs": 0,
            "skipped_existing": [],
            "prompt_path": None,
            "tailored_files": [],
            "notion_pages_updated": 0,
            "resume_path": resolved_resume_path,
        }

    pending_jobs: List[Dict[str, Any]] = []
    skipped_existing: List[Dict[str, Any]] = []

    for job in jobs:
        existing_file = (
            None
            if force_regenerate
            else find_existing_tailored_resume(job["company"], job["title"], output_dir=output_dir)
        )
        if existing_file is not None:
            job["existing_resume_file"] = existing_file
            skipped_existing.append(job)
        else:
            pending_jobs.append(job)

    if console and skipped_existing:
        for sjob in skipped_existing:
            console.print(
                f"[dim cyan][SKIP] Already tailored:[/dim cyan] "
                f"[bold]{sjob['company']} - {sjob['title']}[/bold] "
                f"(Found: [green]{sjob['existing_resume_file'].name}[/green])"
            )

    if pending_jobs:
        batch_prompt = build_argon_tailoring_prompt(
            jobs=pending_jobs,
            resume_text=resume_text,
            resume_filename=resolved_resume_path.name,
        )
        batch_prompt_path.write_text(batch_prompt, encoding="utf-8")
    else:
        batch_prompt_path = None

    tailored_files: List[Path] = []
    notion_updated = 0

    for job in pending_jobs:
        single_prompt = build_argon_tailoring_prompt(
            jobs=[job],
            resume_text=resume_text,
            resume_filename=resolved_resume_path.name,
        )
        slug = f"{_slugify(job['company'])}_{_slugify(job['title'])}"
        job_prompt_file = output_dir / f"{today_str}_{slug}_Argon_Prompt.txt"
        job_prompt_file.write_text(single_prompt, encoding="utf-8")
        tailored_files.append(job_prompt_file)

        ai_output = tailor_with_gemini_api(
            prompt=single_prompt,
            gemini_api_key=gemini_api_key,
            http_session=http_session,
        )
        if ai_output:
            tailored_txt = output_dir / f"{today_str}_{slug}_Tailored_Resume.txt"
            tailored_txt.write_text(ai_output, encoding="utf-8")
            tailored_files.append(tailored_txt)

        if update_notion_pages and job.get("notion_page_id"):
            ok = write_tailoring_to_notion_page(
                page_id=job["notion_page_id"],
                job=job,
                single_job_prompt=single_prompt,
                prompt_file_path=job_prompt_file,
                tailored_output=ai_output,
                api_key=api_key,
                http_session=http_session,
            )
            if ok:
                notion_updated += 1

    if console:
        console.print(
            f"[bold green]Tailoring Queue Summary:[/bold green] "
            f"{len(pending_jobs)} new to tailor, {len(skipped_existing)} already tailored (skipped) "
            f"(Notion cards updated: {notion_updated})"
        )

    return {
        "jobs_found": len(jobs),
        "pending_jobs": len(pending_jobs),
        "skipped_existing": skipped_existing,
        "prompt_path": batch_prompt_path,
        "tailored_files": tailored_files,
        "notion_pages_updated": notion_updated,
        "resume_path": resolved_resume_path,
    }


def main() -> None:
    """CLI entrypoint to process all 'Want to Apply' jobs from Notion."""
    console = Console()
    console.print("[bold cyan]Scanning Notion 'Want to Apply' Queue for Argon Resume Tailoring...[/bold cyan]")

    engine = init_db()
    SessionLocal = get_session_factory(engine)

    with SessionLocal() as session:
        result = process_want_to_apply_queue(session=session, console=console)

    if result["jobs_found"] == 0:
        console.print(
            "[yellow]No cards currently have Status = 'Want to Apply' on your Notion board.[/yellow]\n"
            "Tip: Change any job's Status to [bold]Want to Apply[/bold] in Notion, then re-run!"
        )
    elif result["pending_jobs"] == 0:
        console.print(
            "[bold green]All 'Want to Apply' jobs already have tailored resumes in data/tailored/![/bold green]"
        )
    else:
        console.print(
            f"[bold green]Ready![/bold green] Master Argon Prompt saved to: {result['prompt_path']}"
        )


if __name__ == "__main__":
    main()
