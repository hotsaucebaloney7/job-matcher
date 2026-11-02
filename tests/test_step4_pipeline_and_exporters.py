"""Automated QA test suite for Step 4: Adzuna Client, Markdown Digest, Notion Sync, and End-to-End Pipeline."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

from src.db.models import JobPosting
from src.db.session import get_engine, get_session_factory, init_db, upsert_job_postings
from src.exporters.markdown_digest import (
    format_salary_range,
    generate_markdown_digest,
    get_high_scoring_new_postings,
)
from src.exporters.notion_sync import sync_to_notion
from src.ingest.adzuna_client import poll_adzuna
from src.main import run_pipeline
from src.scoring.embed_scorer import EmbeddingScorer
from src.scoring.filters import compute_job_hash
from tests.test_step3_scoring import DummyEncoder


class TestAdzunaAggregatorClient:
    """Verify Adzuna Local (Raleigh 24km) and US-Remote ingestion client."""

    def test_adzuna_skips_cleanly_without_credentials(self) -> None:
        jobs, stats = poll_adzuna(app_id="", api_key="")
        assert jobs == []
        assert stats == []

    def test_adzuna_polls_and_filters_local_and_remote_queries(self) -> None:
        mock_session = MagicMock()
        local_resp = MagicMock()
        local_resp.json.return_value = {
            "results": [
                {
                    "id": "adz-local-1",
                    "title": "<strong>Senior Data Scientist</strong>",
                    "company": {"display_name": "Red Hat"},
                    "location": {"display_name": "Raleigh, Wake County", "area": ["US", "NC", "Raleigh"]},
                    "salary_min": 145000,
                    "salary_max": 175000,
                    "redirect_url": "https://www.adzuna.com/details/adz-local-1",
                    "description": "Python, SQL, AWS, and Machine Learning in Raleigh, NC.",
                    "created": "2026-11-01T10:00:00Z",
                },
                {
                    "id": "adz-local-2",
                    "title": "Director of Data Science",  # Knocked out by seniority
                    "company": {"display_name": "Red Hat"},
                    "location": {"display_name": "Raleigh, NC"},
                    "redirect_url": "https://www.adzuna.com/details/adz-local-2",
                    "description": "Director role.",
                },
            ]
        }

        remote_resp = MagicMock()
        remote_resp.json.return_value = {
            "results": [
                {
                    "id": "adz-rem-1",
                    "title": "Applied AI Engineer",
                    "company": {"display_name": "RemoteCo"},
                    "location": {"display_name": "Remote, US", "area": ["US"]},
                    "salary_min": 160000,
                    "salary_max": 200000,
                    "redirect_url": "https://www.adzuna.com/details/adz-rem-1",
                    "description": "Build RAG and LLM agents with PyTorch.",
                    "created": "2026-11-01T15:00:00Z",
                }
            ]
        }

        mock_session.get.side_effect = [local_resp, remote_resp]

        jobs, stats = poll_adzuna(app_id="test_id", api_key="test_key", session=mock_session)
        assert len(stats) == 2
        assert len(jobs) == 2

        by_company = {j["company"]: j for j in jobs}
        assert by_company["Red Hat"]["workplace_type"] == "Local"
        assert by_company["Red Hat"]["salary_min"] == 145000
        assert by_company["RemoteCo"]["workplace_type"] == "Remote"


class TestMarkdownDigestAndNotionExporters:
    """Verify Stage 4 query filtering, Markdown digest generation, and Notion sync."""

    def test_format_salary_range(self) -> None:
        assert format_salary_range(140000, 180000) == "$140,000 - $180,000"
        assert format_salary_range(150000, 150000) == "$150,000"
        assert format_salary_range(130000, None) == "$130,000+"
        assert format_salary_range(None, None) == "Not listed"

    def test_markdown_digest_and_notion_sync(self, tmp_path: Path) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        SessionLocal = get_session_factory(engine)

        with SessionLocal() as session:
            h_high = compute_job_hash("GitLab", "Data Scientist", "Remote")
            h_low = compute_job_hash("OtherCorp", "Data Engineer", "Local")
            upsert_job_postings(
                session,
                [
                    {
                        "job_hash": h_high,
                        "source": "greenhouse",
                        "external_id": "10",
                        "company": "GitLab",
                        "title": "Data Scientist",
                        "location_raw": "Remote, United States",
                        "workplace_type": "Remote",
                        "salary_min": 150000,
                        "salary_max": 190000,
                        "url": "https://boards.greenhouse.io/gitlab/jobs/10",
                        "description_text": "Python, SQL, AWS, Machine Learning, PyTorch, RAG.",
                        "date_posted": datetime(2026, 11, 1, 12, 0, 0),
                    },
                    {
                        "job_hash": h_low,
                        "source": "adzuna",
                        "external_id": "20",
                        "company": "OtherCorp",
                        "title": "Data Engineer",
                        "location_raw": "Raleigh, NC",
                        "workplace_type": "Local",
                        "url": "https://example.com/jobs/20",
                        "description_text": "Unrelated description.",
                    },
                ],
            )

            high_row = session.get(JobPosting, h_high)
            low_row = session.get(JobPosting, h_low)
            assert high_row is not None and low_row is not None

            high_row.keyword_score = 80.0
            high_row.semantic_score = 76.0
            high_row.match_score = 78.0
            high_row.matched_keywords = {
                "matched_groups": {"Python Ecosystem": ["python"], "SQL & Databases": ["sql"]},
                "missing_groups": ["Backend / APIs"],
            }

            low_row.keyword_score = 20.0
            low_row.semantic_score = 40.0
            low_row.match_score = 30.0
            session.commit()

            qualifying = get_high_scoring_new_postings(session, min_score=65.0)
            assert len(qualifying) == 1
            assert qualifying[0].company == "GitLab"

            report_file = generate_markdown_digest(
                qualifying,
                reports_dir=tmp_path,
                report_date=date(2026, 11, 2),
                min_score=65.0,
            )
            assert report_file.name == "daily_matches_2026-11-02.md"
            content = report_file.read_text(encoding="utf-8")
            assert "[REMOTE]" in content
            assert "[GitLab — Data Scientist](https://boards.greenhouse.io/gitlab/jobs/10)" in content
            assert "$150,000 - $190,000" in content
            assert "`Python Ecosystem` (python)" in content
            assert "`Backend / APIs`" in content

            # Verify Notion Sync records notion_page_id
            mock_http = MagicMock()
            mock_post_resp = MagicMock()
            mock_post_resp.json.return_value = {"id": "notion-page-uuid-123"}
            mock_http.post.return_value = mock_post_resp

            synced = sync_to_notion(
                session,
                min_score=65.0,
                api_key="secret_test",
                database_id="db_test",
                http_session=mock_http,
            )
            assert synced == 1
            assert high_row.notion_page_id == "notion-page-uuid-123"


class TestEndToEndPipelineOrchestrator:
    """Verify src/main.py run_pipeline end-to-end execution."""

    def test_run_pipeline_scores_and_exports_markdown_report(self, tmp_path: Path) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        SessionLocal = get_session_factory(engine)

        with SessionLocal() as session:
            upsert_job_postings(
                session,
                [
                    {
                        "job_hash": compute_job_hash("RaleighAI", "AI Engineer", "Local"),
                        "source": "greenhouse",
                        "external_id": "555",
                        "company": "RaleighAI",
                        "title": "AI Engineer",
                        "location_raw": "Raleigh, NC",
                        "workplace_type": "Local",
                        "salary_min": 165000,
                        "salary_max": 195000,
                        "url": "https://boards.greenhouse.io/raleighai/jobs/555",
                        "description_text": (
                            "Python, Pandas, SQL, PostgreSQL, AWS, SageMaker, "
                            "Machine Learning, PyTorch, RAG, LLM, FastAPI, Docker, M.S."
                        ),
                    }
                ],
            )

        dummy_embed = EmbeddingScorer(
            resume_text="Python PyTorch RAG Engineer",
            model=DummyEncoder(),
        )
        report_path = run_pipeline(
            engine=engine,
            reports_dir=tmp_path,
            min_score=65.0,
            skip_ingest=True,
            report_date=date(2026, 11, 2),
            embed_scorer=dummy_embed,
        )
        assert report_path.exists()
        md_text = report_path.read_text(encoding="utf-8")
        assert "RaleighAI" in md_text
        assert "`[LOCAL]`" in md_text
