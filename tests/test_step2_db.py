"""Automated QA test suite for Step 2: SQLAlchemy 2.0 Schema, Deduplicating Upsert, and Unscored Query."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Generator

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from src.db.models import JobPosting
from src.db.session import get_engine, get_session_factory, get_unscored_postings, init_db, upsert_job_postings
from src.scoring.filters import compute_job_hash


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Provide an isolated in-memory database session for fast, deterministic unit tests."""
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    SessionLocal = get_session_factory(engine)
    with SessionLocal() as session:
        yield session


def _make_sample_posting(
    company: str = "Anthropic",
    title: str = "AI Engineer",
    workplace_type: str = "Remote",
    url: str = "https://boards.greenhouse.io/anthropic/jobs/999",
    salary_min: int | None = None,
    salary_max: int | None = None,
) -> Dict[str, Any]:
    return {
        "job_hash": compute_job_hash(company, title, workplace_type),
        "source": "greenhouse",
        "external_id": "999",
        "company": company,
        "title": title,
        "location_raw": "Remote - US",
        "workplace_type": workplace_type,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "url": url,
        "description_text": "Build production LLM and RAG systems with Python and PyTorch.",
        "date_posted": datetime(2026, 11, 1, 12, 0, 0),
    }


class TestStep2DatabaseSchemaAndUpsert:
    """Verify Stage 2 ORM schema, deduplication, and unscored row filtering."""

    def test_job_postings_table_columns_and_indexes(self, db_session: Session) -> None:
        inspector = inspect(db_session.bind)
        assert "job_postings" in inspector.get_table_names()

        columns = {col["name"] for col in inspector.get_columns("job_postings")}
        expected_columns = {
            "job_hash",
            "source",
            "external_id",
            "company",
            "title",
            "location_raw",
            "workplace_type",
            "salary_min",
            "salary_max",
            "url",
            "description_text",
            "date_posted",
            "date_fetched",
            "date_last_seen",
            "keyword_score",
            "semantic_score",
            "match_score",
            "matched_keywords",
            "status",
            "notion_page_id",
        }
        assert expected_columns.issubset(columns)

    def test_upsert_inserts_and_deduplicates_on_job_hash(self, db_session: Session) -> None:
        posting = _make_sample_posting()
        early_time = datetime.utcnow() - timedelta(days=1)
        posting["date_last_seen"] = early_time

        count1 = upsert_job_postings(db_session, [posting])
        assert count1 == 1

        total = db_session.scalar(select(func.count()).select_from(JobPosting))
        assert total == 1

        row = db_session.get(JobPosting, posting["job_hash"])
        assert row is not None
        assert row.status == "new"
        assert row.match_score is None

        # Simulate scoring the row and changing its status
        row.keyword_score = 80.0
        row.semantic_score = 84.0
        row.match_score = 82.0
        row.status = "reviewed"
        db_session.commit()

        # Re-upsert the same posting with newly discovered salary info
        updated_posting = _make_sample_posting(salary_min=160000, salary_max=210000)
        count2 = upsert_job_postings(db_session, [updated_posting])
        assert count2 == 1

        # Total row count must remain 1, scores/status must be preserved, and salary/date_last_seen updated
        total_after = db_session.scalar(select(func.count()).select_from(JobPosting))
        assert total_after == 1

        refreshed = db_session.get(JobPosting, posting["job_hash"])
        assert refreshed is not None
        assert refreshed.match_score == 82.0
        assert refreshed.status == "reviewed"
        assert refreshed.salary_min == 160000
        assert refreshed.salary_max == 210000
        assert refreshed.date_last_seen > early_time

    def test_get_unscored_postings_filters_scored_rows(self, db_session: Session) -> None:
        p1 = _make_sample_posting(
            company="CompanyA",
            title="Data Scientist",
            url="https://example.com/jobs/1",
        )
        p2 = _make_sample_posting(
            company="CompanyB",
            title="AI Engineer",
            url="https://example.com/jobs/2",
        )
        upsert_job_postings(db_session, [p1, p2])

        # Score p1 only
        row1 = db_session.get(JobPosting, p1["job_hash"])
        assert row1 is not None
        row1.match_score = 75.5
        db_session.commit()

        unscored = get_unscored_postings(db_session)
        assert len(unscored) == 1
        assert unscored[0].job_hash == p2["job_hash"]
