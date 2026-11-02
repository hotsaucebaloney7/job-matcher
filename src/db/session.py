"""Database engine initialization, session factory, and deduplicating upsert helpers."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import Base, JobPosting

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=ENV_PATH)

DEFAULT_DATABASE_URL = "mysql+pymysql://jobmatcher:jobmatcher_pass@localhost:3306/job_matcher?charset=utf8mb4"


def get_database_url(override_url: Optional[str] = None) -> str:
    """Resolve the SQLAlchemy database URL from argument or .env."""
    if override_url:
        return override_url
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def get_engine(database_url: Optional[str] = None, echo: bool = False) -> Engine:
    """Create a SQLAlchemy 2.0 Engine with connection pre-ping enabled."""
    url = get_database_url(database_url)
    return create_engine(url, echo=echo, pool_pre_ping=True)


def get_session_factory(engine: Optional[Engine] = None) -> sessionmaker[Session]:
    """Create a configured SQLAlchemy Session factory."""
    target_engine = engine or get_engine()
    return sessionmaker(bind=target_engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db(engine: Optional[Engine] = None) -> Engine:
    """Create the job_postings table and indexes if they do not exist."""
    target_engine = engine or get_engine()
    Base.metadata.create_all(bind=target_engine)
    return target_engine


def _normalize_row_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare a dictionary matching JobPosting columns for bulk/single upsert."""
    now = datetime.utcnow()
    return {
        "job_hash": raw["job_hash"],
        "source": raw["source"],
        "external_id": raw.get("external_id"),
        "company": raw["company"],
        "title": raw["title"],
        "location_raw": raw.get("location_raw"),
        "workplace_type": raw["workplace_type"],
        "salary_min": raw.get("salary_min"),
        "salary_max": raw.get("salary_max"),
        "url": raw["url"],
        "description_text": raw.get("description_text") or "",
        "date_posted": raw.get("date_posted"),
        "date_fetched": raw.get("date_fetched") or now,
        "date_last_seen": raw.get("date_last_seen") or now,
    }


def upsert_job_postings(session: Session, postings: Sequence[Dict[str, Any]]) -> int:
    """Upsert normalized job postings using deterministic job_hash deduplication.

    On duplicate key (job_hash or unique url), updates date_last_seen = NOW()
    without overwriting existing scoring or review status fields.

    Returns:
        Number of postings processed in the upsert batch.
    """
    if not postings:
        return 0

    # Deduplicate within the incoming batch by both job_hash and url
    seen_hashes = set()
    seen_urls = set()
    rows: List[Dict[str, Any]] = []
    for item in postings:
        j_hash = item.get("job_hash")
        url = item.get("url")
        if not j_hash or not url:
            continue
        if j_hash in seen_hashes or url in seen_urls:
            continue
        seen_hashes.add(j_hash)
        seen_urls.add(url)
        rows.append(_normalize_row_payload(item))

    if not rows:
        return 0

    dialect_name = session.bind.dialect.name if session.bind is not None else "mysql"

    if dialect_name == "mysql":
        stmt = mysql_insert(JobPosting).values(rows)
        upsert_stmt = stmt.on_duplicate_key_update(
            date_last_seen=func.now(),
            salary_min=func.coalesce(stmt.inserted.salary_min, JobPosting.salary_min),
            salary_max=func.coalesce(stmt.inserted.salary_max, JobPosting.salary_max),
        )
        session.execute(upsert_stmt)
    elif dialect_name == "sqlite":
        for row in rows:
            stmt = sqlite_insert(JobPosting).values(row)
            upsert_stmt = stmt.on_conflict_do_update(
                index_elements=["job_hash"],
                set_={
                    "date_last_seen": datetime.utcnow(),
                    "salary_min": func.coalesce(stmt.excluded.salary_min, JobPosting.salary_min),
                    "salary_max": func.coalesce(stmt.excluded.salary_max, JobPosting.salary_max),
                },
            )
            session.execute(upsert_stmt)
    else:
        for row in rows:
            existing = session.get(JobPosting, row["job_hash"])
            if existing:
                existing.date_last_seen = datetime.utcnow()
            else:
                session.add(JobPosting(**row))

    session.commit()
    session.expire_all()
    return len(rows)


def get_unscored_postings(session: Session) -> List[JobPosting]:
    """Return all job postings where match_score IS NULL (awaiting Stage 3 scoring)."""
    stmt = select(JobPosting).where(JobPosting.match_score.is_(None)).order_by(JobPosting.date_fetched.desc())
    return list(session.scalars(stmt).all())


def main() -> None:
    """CLI entrypoint to verify MySQL 8.0 table creation and deduplicating upsert."""
    from src.ingest.ats_poller import poll_all_ats

    console = Console()
    console.print("[bold cyan]Stage 2: Initializing MySQL 8.0 schema and verifying upsert...[/bold cyan]")

    engine = init_db()
    SessionLocal = get_session_factory(engine)

    jobs, _ = poll_all_ats(console=console)
    console.print(f"Fetched [bold]{len(jobs)}[/bold] filtered postings from ATS boards.")

    with SessionLocal() as session:
        processed_first = upsert_job_postings(session, jobs)
        total_after_first = session.scalar( select(func.count()).select_from(JobPosting) )

        # Run a second upsert pass to verify ON DUPLICATE KEY UPDATE deduplication
        processed_second = upsert_job_postings(session, jobs)
        total_after_second = session.scalar( select(func.count()).select_from(JobPosting) )
        unscored = get_unscored_postings(session)

        console.print(
            f"[green]Pass 1 Upserted:[/green] {processed_first} -> Total DB Rows: {total_after_first}\n"
            f"[green]Pass 2 Upserted (Deduplication Check):[/green] {processed_second} -> Total DB Rows: {total_after_second}\n"
            f"[cyan]Rows awaiting Stage 3 scoring (match_score IS NULL):[/cyan] {len(unscored)}"
        )

        table = Table(title="MySQL job_postings Table Verification", show_lines=False)
        table.add_column("Hash (8)", style="magenta")
        table.add_column("Source", style="cyan")
        table.add_column("Company", style="bold")
        table.add_column("Title")
        table.add_column("Mode", justify="center")
        table.add_column("Status", style="green")
        table.add_column("Last Seen (UTC)", style="dim")

        for row in unscored[:15]:
            table.add_row(
                row.job_hash[:8],
                row.source,
                row.company,
                row.title,
                row.workplace_type,
                row.status,
                str(row.date_last_seen),
            )
        console.print(table)


if __name__ == "__main__":
    main()
