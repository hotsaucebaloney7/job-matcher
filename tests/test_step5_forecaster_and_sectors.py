"""Automated QA test suite for Step 5: Notion Transition Logging, Undo, Offer Forecaster, and Nuclear/Commodity Expansion."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy import select

from src.analytics.forecaster import (
    format_banner_summary,
    run_offer_forecast,
    write_forecast_markdown,
)
from src.db.models import ApplicationEvent, JobPosting
from src.db.session import get_engine, get_session_factory, init_db, upsert_job_postings
from src.exporters.notion_sync import sync_notion_board_transitions, undo_last_notion_transition
from src.ingest.ats_poller import fetch_oracle_hcm_board, fetch_smartrecruiters_board, parse_xml_job_feed
from src.ingest.external_apis import poll_jooble
from src.scoring.filters import (
    classify_location,
    classify_sector,
    compute_job_hash,
    evaluate_posting,
    format_location_tag,
    is_excluded_title,
    is_target_title,
)
from src.scoring.keyword_scorer import KeywordScorer


class TestNuclearEnergyCommodityExpansion:
    """Verify Wilmington NC geography, quantitative/energy titles, Sector classification, and domain boosts."""

    def test_wilmington_nc_accepted_and_delaware_rejected(self) -> None:
        assert classify_location("Wilmington, NC") == "Local"
        assert classify_location("Wilmington, North Carolina") == "Local"
        assert classify_location("USA NC Wilmington") == "Local"
        assert format_location_tag("Wilmington, NC", "Local") == "Wilmington NC"
        assert classify_location("Wilmington, DE") is None

    def test_energy_and_quant_titles_and_sector_classifier(self) -> None:
        assert is_target_title("Quantitative Analyst - Power Markets")
        assert is_target_title("Energy Data Scientist")
        assert is_target_title("Analytics Engineer")
        assert classify_sector("GE Vernova", "Data Scientist", "BWRX-300 nuclear SMR reactor telemetry") == "Nuclear / Energy"
        assert classify_sector("X-energy", "Data Engineer", "Small modular reactor") == "Nuclear / Energy"
        assert classify_sector("Vitol", "Quantitative Analyst", "Power and commodity trading PJM") == "Commodity / Quant"
        assert classify_sector("Booz Allen Hamilton", "Data Scientist", "DoD TS/SCI analytics") == "Defense / Federal"
        assert classify_sector("Stripe", "Data Scientist", "Payments analytics") == "Tech"

    def test_energy_and_commodity_domain_boost_is_non_penalizing(self) -> None:
        scorer = KeywordScorer()
        base_score, base_info = scorer.score("Python, SQL, PyTorch, ETL Pipeline")
        boosted_score, boosted_info = scorer.score(
            "Python, SQL, PyTorch, ETL Pipeline for Nuclear SMR Power Grid SCADA & PJM Commodity Trading"
        )
        assert boosted_score > base_score
        assert "Nuclear & Power Systems" not in base_info["missing_groups"]
        assert "Energy & Commodity Markets" not in base_info["missing_groups"]
        assert boosted_info["tier_scores"]["domain_energy_commodity_boost"] == 15.0

    def test_smartrecruiters_oracle_hcm_and_jooble_parsers(self) -> None:
        # SmartRecruiters mock
        mock_sess = MagicMock()
        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = {
            "content": [
                {
                    "id": "sr-1",
                    "name": "Quantitative Analyst",
                    "location": {"city": "Washington", "region": "DC", "country": "US", "remote": False},
                    "releasedDate": "2026-11-20T10:00:00Z",
                }
            ]
        }
        det_resp = MagicMock()
        det_resp.status_code = 200
        det_resp.json.return_value = {
            "applyUrl": "https://jobs.smartrecruiters.com/Vitol/sr-1",
            "jobAd": {"sections": {"jobDescription": {"text": "<p>Python and PJM power trading.</p>"}}},
        }
        mock_sess.get.side_effect = [list_resp, det_resp]
        sr_jobs, sr_raw = fetch_smartrecruiters_board("Vitol", "Vitol", mock_sess)
        assert sr_raw == 1
        assert len(sr_jobs) == 1
        assert sr_jobs[0]["source"] == "smartrecruiters"

        # Oracle HCM mock
        ora_sess = MagicMock()
        ora_resp = MagicMock()
        ora_resp.status_code = 200
        ora_resp.json.return_value = {
            "items": [
                {
                    "requisitionList": [
                        {
                            "Id": "9001",
                            "Title": "Data Scientist",
                            "PrimaryLocation": "Baltimore, MD, United States",
                            "ShortDescriptionStr": "Nuclear fleet predictive maintenance in Python.",
                            "PostedDate": "2026-11-20",
                        }
                    ]
                }
            ]
        }
        ora_sess.get.return_value = ora_resp
        ora_jobs, ora_raw = fetch_oracle_hcm_board(
            "https://example.fa.us2.oraclecloud.com",
            "CX_1",
            "Constellation Energy",
            ora_sess,
            search_terms=("Data Scientist",),
        )
        assert ora_raw == 1
        assert len(ora_jobs) == 1
        assert ora_jobs[0]["source"] == "oracle_hcm"

        # Jooble mock
        j_sess = MagicMock()
        j_resp = MagicMock()
        j_resp.status_code = 200
        j_resp.json.return_value = {
            "jobs": [
                {
                    "id": "jb-1",
                    "title": "Energy Data Scientist",
                    "company": "GE Hitachi Nuclear",
                    "location": "Wilmington, NC",
                    "link": "https://careers.gevernova.com/job/jb-1",
                    "snippet": "Python and nuclear reactor analytics.",
                    "updated": "2026-11-22T12:00:00Z",
                }
            ]
        }
        j_sess.post.return_value = j_resp
        j_jobs, _ = poll_jooble(api_key="test_jooble_key", session=j_sess)
        assert len(j_jobs) == 1
        assert j_jobs[0]["source"] == "jooble"


class TestNotionTransitionLoggingUndoAndForecaster:
    """Verify automatic Notion status-transition logging, date auto-stamping, Undo rollback, and Monte Carlo forecast."""

    def test_transition_sync_auto_stamping_and_undo(self) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        SessionLocal = get_session_factory(engine)

        j_hash = compute_job_hash("GE Vernova", "Data Scientist", "Local")
        with SessionLocal() as session:
            upsert_job_postings(
                session,
                [
                    {
                        "job_hash": j_hash,
                        "source": "workday",
                        "external_id": "R900",
                        "company": "GE Vernova",
                        "title": "Data Scientist",
                        "location_raw": "Wilmington, NC",
                        "workplace_type": "Local",
                        "url": "https://gevernova.wd5.myworkdayjobs.com/job/R900",
                        "description_text": "Nuclear SMR Python data science.",
                    }
                ],
            )
            row = session.get(JobPosting, j_hash)
            assert row is not None
            row.notion_page_id = "page-ge-1"
            row.notion_status = "Want to Apply"
            row.match_score = 84.0
            session.commit()

            mock_http = MagicMock()
            db_get = MagicMock(status_code=200)
            db_get.json.return_value = {"properties": {"Name": {"type": "title"}}}
            mock_http.get.return_value = db_get

            # Simulate user dragging card from Want to Apply -> Applied (without filling in Date Applied)
            q_post = MagicMock(status_code=200)
            q_post.json.return_value = {
                "has_more": False,
                "results": [
                    {
                        "id": "page-ge-1",
                        "archived": False,
                        "last_edited_time": "2026-11-24T14:00:00.000Z",
                        "properties": {
                            "Name": {"title": [{"plain_text": "GE Vernova — Data Scientist"}]},
                            "Status": {"select": {"name": "Applied"}},
                            "Priority": {"select": {"name": "Tier 1"}},
                            "Match Score": {"number": 84.0},
                            "URL": {"url": "https://gevernova.wd5.myworkdayjobs.com/job/R900"},
                            "Date Applied": {"date": None},
                            "Follow-Up Date": {"date": None},
                        },
                    }
                ],
            }
            mock_http.post.return_value = q_post
            mock_http.patch.return_value = MagicMock(status_code=200)

            res = sync_notion_board_transitions(
                session,
                api_key="secret_test",
                database_id="40c3be74ca2c80a99786e33cd4b0ee89",
                http_session=mock_http,
            )
            assert res["events_logged"] == 1
            assert res["dates_stamped"] == 1
            assert row.notion_status == "Applied"
            assert row.sector == "Nuclear / Energy"
            assert row.date_applied is not None

            events = list(session.scalars(select(ApplicationEvent)).all())
            assert len(events) == 1
            assert events[0].from_status == "Want to Apply"
            assert events[0].to_status == "Applied"

            # Now test Undo rollback
            reverted = undo_last_notion_transition(
                session,
                steps=1,
                api_key="secret_test",
                http_session=mock_http,
            )
            assert len(reverted) == 1
            assert reverted[0]["reverted_to"] == "Want to Apply"
            assert row.notion_status == "Want to Apply"
            assert len(list(session.scalars(select(ApplicationEvent)).all())) == 0

    def test_monte_carlo_offer_forecaster_and_markdown_output(self, tmp_path: Path) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        SessionLocal = get_session_factory(engine)

        ref_day = date(2026, 11, 24)
        target_day = date(2027, 2, 14)

        with SessionLocal() as session:
            # Seed 10 in-flight Tier 1 applications submitted over the last 14 days
            postings = []
            for i in range(10):
                h = compute_job_hash(f"NuclearCorp{i}", "Data Scientist", "Local")
                postings.append(
                    {
                        "job_hash": h,
                        "source": "workday",
                        "external_id": f"R{i}",
                        "company": f"NuclearCorp{i}",
                        "title": "Data Scientist",
                        "location_raw": "Wilmington, NC",
                        "workplace_type": "Local",
                        "url": f"https://example.com/job/{i}",
                        "description_text": "Nuclear reactor Python data science.",
                    }
                )
            upsert_job_postings(session, postings)
            for idx, r in enumerate(session.scalars(select(JobPosting)).all()):
                r.notion_page_id = f"pg-{idx}"
                r.notion_status = "Applied"
                r.sector = "Nuclear / Energy"
                r.match_score = 82.0
                r.date_applied = datetime(2026, 11, 14) + timedelta(days=idx)
            session.commit()

            low_pace = run_offer_forecast(
                session,
                reference_date=ref_day,
                target_date=target_day,
                override_weekly_pace=5.0,
                n_sims=2000,
                seed=7,
            )
            high_pace = run_offer_forecast(
                session,
                reference_date=ref_day,
                target_date=target_day,
                override_weekly_pace=15.0,
                n_sims=2000,
                seed=7,
            )

            assert high_pace.prob_by_target >= low_pace.prob_by_target
            assert high_pace.p25_date is not None and high_pace.p50_date is not None and high_pace.p80_date is not None
            assert high_pace.p25_date <= high_pace.p50_date <= high_pace.p80_date
            assert "Median Written Offer:" in format_banner_summary(high_pace)

            md_path = write_forecast_markdown(high_pace, output_path=tmp_path / "offer_forecast.md")
            assert md_path.exists()
            text = md_path.read_text(encoding="utf-8")
            assert "Nuclear / Energy" in text
            assert "February 14, 2027" in text


class TestLawEnforcementExpansion:
    """Verify analytical-only law enforcement filtering, Law Enforcement sector tag, and NEOGOV RSS parsing."""

    def test_sworn_law_enforcement_roles_are_knocked_out(self) -> None:
        for title in (
            "Police Officer",
            "Deputy Sheriff",
            "Special Agent (Criminal Investigator)",
            "Border Patrol Agent",
            "State Trooper",
            "Correctional Officer",
        ):
            assert is_excluded_title(title), f"Expected sworn title to be excluded: {title}"

    def test_analytical_law_enforcement_titles_and_technical_gate(self) -> None:
        # Technical Crime Analyst passes
        passed, wp, _, reason = evaluate_posting(
            company="City of Raleigh NC",
            title="Crime Analyst",
            location_raw="Raleigh, NC",
            description_text="Analyze NIBRS crime patterns using SQL, Python, and ArcGIS for the Police Department.",
        )
        assert passed, reason
        assert wp == "Local"

        # Non-technical clerical Crime Analyst is rejected by the technical gate
        passed_non_tech, _, _, reason_non_tech = evaluate_posting(
            company="City of Raleigh NC",
            title="Crime Analyst",
            location_raw="Raleigh, NC",
            description_text="Answer phones, file paper incident reports, and greet visitors at the front desk.",
        )
        assert not passed_non_tech
        assert "technical/analytical" in reason_non_tech.lower()

        # Core technical LE roles pass directly
        passed_aml, _, _, _ = evaluate_posting(
            company="Chainalysis",
            title="Financial Crimes Data Scientist",
            location_raw="Washington, DC",
            description_text="Build Python and PyTorch models for blockchain analytics and anti-money laundering.",
        )
        assert passed_aml

    def test_law_enforcement_sector_classification_across_all_4_varieties(self) -> None:
        # 1. Federal LE
        assert classify_sector("Federal Bureau of Investigation", "Intelligence Analyst", "DOJ investigations", "usajobs") == "Law Enforcement"
        assert classify_sector("Customs and Border Protection", "Data Scientist", "DHS analytics", "usajobs") == "Law Enforcement"
        # 2. State / Local LE
        assert classify_sector("City of Raleigh NC", "Crime Analyst", "[Department: Police Department]\nSQL and GIS crime analytics", "xml_feed") == "Law Enforcement"
        # 3. LE Tech Vendors
        assert classify_sector("Axon", "Machine Learning Engineer", "Computer vision for public safety", "greenhouse") == "Law Enforcement"
        assert classify_sector("Chainalysis", "Data Scientist", "Crypto tracing", "ashby") == "Law Enforcement"
        assert classify_sector("TRM Labs", "Data Engineer", "Blockchain intelligence", "ashby") == "Law Enforcement"
        assert classify_sector("Babel Street", "AI Engineer", "OSINT analytics", "greenhouse") == "Law Enforcement"
        # 4. LE Contractors & Financial Crime / AML
        assert classify_sector("CACI", "Data Scientist", "Support DHS Cyber Crimes Center digital forensics", "workday") == "Law Enforcement"
        assert classify_sector("Guidehouse", "Data Scientist", "Anti-money laundering and financial crimes analytics", "workday") == "Law Enforcement"

    def test_neogov_rss_feed_with_bom_department_and_pubdate(self) -> None:
        neogov_xml = (
            '\ufeff<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0" xmlns:joblisting="http://www.neogov.com/namespaces/JobListing">'
            "<channel><title>City of Raleigh</title>"
            "<item>"
            "<guid>https://www.governmentjobs.com/careers/raleighnc/jobs/999</guid>"
            "<link>https://www.governmentjobs.com/careers/raleighnc/jobs/999</link>"
            "<title>Crime Analyst</title>"
            "<pubDate>Tue, 24 Nov 2026 10:00:00 -0500</pubDate>"
            "<joblisting:department>Police Department</joblisting:department>"
            "<description>Use SQL, Python, and ArcGIS to perform tactical crime analytics.</description>"
            "</item>"
            "<item>"
            "<guid>https://www.governmentjobs.com/careers/raleighnc/jobs/1000</guid>"
            "<link>https://www.governmentjobs.com/careers/raleighnc/jobs/1000</link>"
            "<title>Police Officer</title>"
            "<description>Sworn patrol officer duties.</description>"
            "</item>"
            "</channel></rss>"
        )
        matched, raw_count = parse_xml_job_feed(neogov_xml, company_name="City of Raleigh NC", default_location="Raleigh, NC")
        assert raw_count == 2
        assert len(matched) == 1
        assert matched[0]["title"] == "Crime Analyst"
        assert "[Department: Police Department]" in matched[0]["description_text"]
        assert matched[0]["date_posted"] is not None
        assert classify_sector(matched[0]["company"], matched[0]["title"], matched[0]["description_text"], "xml_feed") == "Law Enforcement"
