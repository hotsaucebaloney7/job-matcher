"""Automated QA test suite for Step 4: Adzuna Client, Markdown Digest, Notion Sync, and End-to-End Pipeline."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

from src.db.models import JobPosting
from src.db.session import expire_stale_postings, get_engine, get_session_factory, init_db, upsert_job_postings
from src.exporters.markdown_digest import (
    format_salary_range,
    generate_markdown_digest,
    get_high_scoring_new_postings,
)
from src.exporters.notion_sync import archive_expired_notion_pages, sync_to_notion
from src.exporters.resume_tailor import (
    build_argon_tailoring_prompt,
    fetch_want_to_apply_jobs,
    process_want_to_apply_queue,
)
from src.ingest.adzuna_client import poll_adzuna
from src.ingest.ats_poller import fetch_ashby_board
from src.ingest.external_apis import poll_jsearch_rapidapi, poll_remotive, poll_the_muse, poll_usajobs
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
            enable_notion_sync=False,
        )
        assert report_path.exists()
        md_text = report_path.read_text(encoding="utf-8")
        assert "RaleighAI" in md_text
        assert "`[LOCAL]`" in md_text


class TestAshbyAndExternalAggregatorClients:
    """Verify Ashby ATS parser, Remotive, The Muse, and JSearch (Google for Jobs) clients."""

    def test_fetch_ashby_board_filters_and_parses_compensation(self) -> None:
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "id": "ash-1",
                    "title": "Applied AI Engineer",
                    "isListed": True,
                    "isRemote": True,
                    "location": "United States",
                    "workplaceType": "Remote",
                    "jobUrl": "https://jobs.ashbyhq.com/langchain/ash-1",
                    "descriptionPlain": "2+ years of experience with Python, LangChain, RAG, and LLMs.",
                    "publishedAt": "2026-11-01T10:00:00Z",
                    "compensation": {
                        "summaryComponents": [
                            {
                                "compensationType": "Salary",
                                "interval": "1 YEAR",
                                "minValue": 160000,
                                "maxValue": 200000,
                            }
                        ]
                    },
                }
            ]
        }
        mock_session.get.return_value = mock_resp

        matched, raw_count = fetch_ashby_board("langchain", "LangChain", mock_session)
        assert raw_count == 1
        assert len(matched) == 1
        assert matched[0]["source"] == "ashby"
        assert matched[0]["salary_min"] == 160000
        assert matched[0]["salary_max"] == 200000

    def test_poll_remotive_and_the_muse_and_jsearch(self) -> None:
        mock_session = MagicMock()

        # 1. Remotive mock
        rem_resp = MagicMock()
        rem_resp.json.return_value = {
            "jobs": [
                {
                    "id": 501,
                    "title": "Data Scientist",
                    "company_name": "RemoteDataCo",
                    "candidate_required_location": "USA Only",
                    "url": "https://remotive.com/remote-jobs/data/501",
                    "description": "<p>2+ years of experience in Python and SQL.</p>",
                    "publication_date": "2026-11-01T09:00:00",
                }
            ]
        }
        mock_session.get.return_value = rem_resp
        rem_jobs, _ = poll_remotive(session=mock_session)
        assert len(rem_jobs) == 1
        assert rem_jobs[0]["source"] == "remotive"

        # 2. The Muse mock
        muse_resp = MagicMock()
        muse_resp.json.return_value = {
            "results": [
                {
                    "id": 601,
                    "name": "Machine Learning Engineer",
                    "company": {"name": "TriangleTech"},
                    "locations": [{"name": "Raleigh, NC"}],
                    "contents": "<p>1-3 years of experience with PyTorch and AWS.</p>",
                    "refs": {"landing_page": "https://www.themuse.com/jobs/601"},
                }
            ]
        }
        mock_session.get.return_value = muse_resp
        muse_jobs, _ = poll_the_muse(session=mock_session)
        assert len(muse_jobs) == 1
        assert muse_jobs[0]["workplace_type"] == "Local"

        # 3. JSearch (Google for Jobs) mock — verifies direct apply_options priority & scraper blocking
        jsearch_resp = MagicMock()
        jsearch_resp.json.return_value = {
            "data": [
                {
                    "job_id": "js-1",
                    "job_title": "AI Engineer",
                    "employer_name": "RTP BioAI",
                    "job_city": "Durham",
                    "job_state": "NC",
                    "job_country": "US",
                    "job_is_remote": False,
                    "job_description": "3+ years of experience building LLM and RAG pipelines.",
                    "job_min_salary": 135000,
                    "job_max_salary": 165000,
                    "job_salary_period": "YEAR",
                    "job_apply_link": "https://www.indeed.com/viewjob?jk=indirect123",
                    "apply_options": [
                        {"publisher": "Indeed", "apply_link": "https://www.indeed.com/viewjob?jk=indirect123", "is_direct": False},
                        {"publisher": "RTP BioAI Careers", "apply_link": "https://careers.rtpbioai.com/jobs/js-1", "is_direct": True},
                    ],
                    "job_required_experience": {"required_experience_in_months": 36},
                },
                {
                    "job_id": "js-blocked",
                    "job_title": "Data Scientist",
                    "employer_name": "StaleScraperCo",
                    "job_city": "Raleigh",
                    "job_state": "NC",
                    "job_country": "US",
                    "job_is_remote": False,
                    "job_description": "2+ years of Python experience.",
                    "job_apply_link": "https://www.learn4good.com/jobs/stale-1",
                },
            ]
        }
        mock_session.get.return_value = jsearch_resp
        js_jobs, _ = poll_jsearch_rapidapi(rapidapi_key="test_rapid_key", session=mock_session)
        assert len(js_jobs) == 1
        assert js_jobs[0]["company"] == "RTP BioAI"
        assert js_jobs[0]["url"] == "https://careers.rtpbioai.com/jobs/js-1"
        assert js_jobs[0]["salary_min"] == 135000

        # 4. USAJOBS mock
        usa_resp = MagicMock()
        usa_resp.json.return_value = {
            "SearchResult": {
                "SearchResultItems": [
                    {
                        "MatchedObjectId": "usa-101",
                        "MatchedObjectDescriptor": {
                            "PositionTitle": "Data Scientist",
                            "OrganizationName": "Environmental Protection Agency",
                            "PositionLocationDisplay": "Durham, North Carolina",
                            "PositionLocation": [{"LocationName": "Durham, North Carolina"}],
                            "PositionURI": "https://www.usajobs.gov/job/usa-101",
                            "PublicationStartDate": "2026-11-01T00:00:00Z",
                            "PositionRemuneration": [
                                {"MinimumRange": "95000", "MaximumRange": "128000", "RateIntervalCode": "PA"}
                            ],
                            "QualificationSummary": "1-3 years experience with Python, SQL, and statistical modeling.",
                            "UserArea": {"Details": {"JobSummary": "EPA RTP Data Scientist role."}},
                        },
                    }
                ]
            }
        }
        mock_session.get.return_value = usa_resp
        usa_jobs, _ = poll_usajobs(api_key="test_usa_key", session=mock_session)
        assert len(usa_jobs) == 1
        assert usa_jobs[0]["source"] == "usajobs"
        assert usa_jobs[0]["workplace_type"] == "Local"
        assert usa_jobs[0]["salary_min"] == 95000
        assert usa_jobs[0]["salary_max"] == 128000

    def test_expire_stale_postings_and_archive_unapplied_notion_pages(self) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        SessionLocal = get_session_factory(engine)

        hash_unapplied = compute_job_hash("OldCorp", "Data Scientist", "Remote")
        hash_applied = compute_job_hash("AppliedCorp", "AI Engineer", "Local")
        hash_active = compute_job_hash("LiveCorp", "ML Engineer", "Remote")

        with SessionLocal() as session:
            upsert_job_postings(
                session,
                [
                    {
                        "job_hash": hash_unapplied,
                        "source": "greenhouse",
                        "external_id": "1",
                        "company": "OldCorp",
                        "title": "Data Scientist",
                        "location_raw": "Remote - US",
                        "workplace_type": "Remote",
                        "url": "https://boards.greenhouse.io/oldcorp/jobs/1",
                        "description_text": "Python and SQL.",
                    },
                    {
                        "job_hash": hash_applied,
                        "source": "greenhouse",
                        "external_id": "2",
                        "company": "AppliedCorp",
                        "title": "AI Engineer",
                        "location_raw": "Raleigh, NC",
                        "workplace_type": "Local",
                        "url": "https://boards.greenhouse.io/appliedcorp/jobs/2",
                        "description_text": "PyTorch and RAG.",
                    },
                    {
                        "job_hash": hash_active,
                        "source": "greenhouse",
                        "external_id": "3",
                        "company": "LiveCorp",
                        "title": "ML Engineer",
                        "location_raw": "Remote - US",
                        "workplace_type": "Remote",
                        "url": "https://boards.greenhouse.io/livecorp/jobs/3",
                        "description_text": "Python and AWS.",
                    },
                ],
            )

            row_unapplied = session.get(JobPosting, hash_unapplied)
            row_applied = session.get(JobPosting, hash_applied)
            assert row_unapplied is not None and row_applied is not None
            row_unapplied.notion_page_id = "page-unapplied-1"
            row_applied.notion_page_id = "page-applied-2"
            session.commit()

            # Only hash_active is still online
            expired = expire_stale_postings(
                session,
                active_hashes={hash_active},
                healthy_sources={"greenhouse"},
            )
            assert len(expired) == 2
            assert row_unapplied.status == "rejected"
            assert row_applied.status == "rejected"

            # Mock Notion GET: page-unapplied-1 is still 'New Match', page-applied-2 is 'Applied'
            mock_http = MagicMock()

            def fake_get(url: str, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                if url.endswith("page-unapplied-1"):
                    resp.json.return_value = {
                        "archived": False,
                        "properties": {"Status": {"select": {"name": "New Match"}}},
                    }
                else:
                    resp.json.return_value = {
                        "archived": False,
                        "properties": {"Status": {"select": {"name": "Applied"}}},
                    }
                return resp

            mock_http.get.side_effect = fake_get
            mock_patch_resp = MagicMock()
            mock_http.patch.return_value = mock_patch_resp

            archived_count = archive_expired_notion_pages(
                session,
                expired_rows=expired,
                api_key="secret_test",
                http_session=mock_http,
            )
            # Only the unapplied card is archived in Notion; the 'Applied' card is preserved!
            assert archived_count == 1
            assert row_unapplied.matched_keywords.get("notion_archived") is True
            assert row_applied.status == "applied"
            assert row_applied.matched_keywords.get("notion_preserved") is True

    def test_want_to_apply_queue_processor_and_argon_prompt_builder(self, tmp_path: Path) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        SessionLocal = get_session_factory(engine)

        job_hash = compute_job_hash("Mistral AI", "Applied AI Engineer", "Remote")
        with SessionLocal() as session:
            upsert_job_postings(
                session,
                [
                    {
                        "job_hash": job_hash,
                        "source": "ashby",
                        "external_id": "mistral-1",
                        "company": "Mistral AI",
                        "title": "Applied AI Engineer",
                        "location_raw": "Remote - US",
                        "workplace_type": "Remote",
                        "url": "https://jobs.ashbyhq.com/mistral.ai/mistral-1",
                        "description_text": "Build production RAG, fine-tuning, and agentic workflows in Python and PyTorch.",
                    }
                ],
            )
            row = session.get(JobPosting, job_hash)
            assert row is not None
            row.notion_page_id = "page-want-to-apply-1"
            row.match_score = 88.5
            row.keyword_score = 92.0
            row.semantic_score = 85.0
            row.matched_keywords = {
                "experience_tier": "1-3 yrs (Target)",
                "matched_groups": {"Python": ["python"], "GenAI_LLM": ["rag", "agentic"]},
                "missing_groups": ["Cloud_AWS", "Docker_K8s"],
            }
            session.commit()

            mock_http = MagicMock()
            # Mock _ensure_database_schema GET and /databases/{id}/query POST
            mock_db_get = MagicMock()
            mock_db_get.status_code = 200
            mock_db_get.json.return_value = {"properties": {"Name": {"type": "title"}}}
            mock_http.get.return_value = mock_db_get

            mock_query_post = MagicMock()
            mock_query_post.status_code = 200
            mock_query_post.json.return_value = {
                "results": [
                    {
                        "id": "page-want-to-apply-1",
                        "archived": False,
                        "properties": {
                            "Name": {"type": "title", "title": [{"plain_text": "Mistral AI — Applied AI Engineer"}]},
                            "Company": {"type": "rich_text", "rich_text": [{"plain_text": "Mistral AI"}]},
                            "URL": {"url": "https://jobs.ashbyhq.com/mistral.ai/mistral-1"},
                            "Match Score": {"number": 88.5},
                            "Workplace": {"select": {"name": "Remote"}},
                        },
                    }
                ]
            }
            mock_http.post.return_value = mock_query_post
            mock_patch = MagicMock()
            mock_patch.status_code = 200
            mock_http.patch.return_value = mock_patch

            jobs = fetch_want_to_apply_jobs(
                session=session,
                api_key="secret_test",
                database_id="40c3be74ca2c80a99786e33cd4b0ee89",
                http_session=mock_http,
            )
            assert len(jobs) == 1
            assert jobs[0]["company"] == "Mistral AI"
            assert "GenAI_LLM" in jobs[0]["matched_groups"]
            assert "Cloud_AWS" in jobs[0]["missing_groups"]

            prompt = build_argon_tailoring_prompt(
                jobs=jobs,
                resume_text="Josh Engelhardt — Data Scientist & AI Engineer skilled in Python, SQL, PyTorch, RAG.",
                resume_filename="Josh_Engelhardt_Resume-v3.docx",
            )
            assert "Mistral AI — Applied AI Engineer" in prompt
            assert "Cloud_AWS, Docker_K8s" in prompt
            assert "Josh_Engelhardt_Resume-v3.docx" in prompt

            fake_resume = tmp_path / "test_resume.txt"
            fake_resume.write_text("Josh Engelhardt — Python & PyTorch AI Engineer", encoding="utf-8")
            out_dir = tmp_path / "tailored"

            res = process_want_to_apply_queue(
                session=session,
                resume_path=fake_resume,
                output_dir=out_dir,
                api_key="secret_test",
                database_id="40c3be74ca2c80a99786e33cd4b0ee89",
                gemini_api_key="",
                update_notion_pages=True,
                http_session=mock_http,
            )
            assert res["jobs_found"] == 1
            assert res["notion_pages_updated"] == 1
            assert res["prompt_path"] is not None and res["prompt_path"].exists()
            assert len(res["tailored_files"]) == 1



