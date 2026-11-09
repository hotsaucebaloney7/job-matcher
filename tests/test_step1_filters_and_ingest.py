"""Automated QA test suite for Step 1: Knockout Filters, SHA-256 Hashing, HTML Stripping, and ATS Poller."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.ingest.ats_poller import (
    fetch_greenhouse_board,
    fetch_lever_board,
    fetch_workday_cxs_board,
    parse_xml_job_feed,
)
from src.scoring.filters import (
    classify_location,
    compute_job_hash,
    evaluate_posting,
    is_excluded_title,
    is_target_title,
    strip_html,
)


class TestTitleKnockoutFilters:
    """Verify Step 3.1 seniority knockouts and target role family matching."""

    @pytest.mark.parametrize(
        "title",
        [
            "Data Scientist",
            "Senior Data Scientist",
            "AI Engineer",
            "Senior AI Engineer",
            "Machine Learning Engineer II",
            "Applied AI Engineer",
            "Data Engineer",
            "MLOps Engineer",
            "ML Ops Engineer",
            "GenAI Engineer",
        ],
    )
    def test_accepted_target_titles(self, title: str) -> None:
        assert not is_excluded_title(title)
        assert is_target_title(title)

    @pytest.mark.parametrize(
        "title",
        [
            "Staff Data Scientist",
            "Principal Machine Learning Engineer",
            "Director of Data Science",
            "VP of AI Engineering",
            "Vice President, Machine Learning",
            "Head of AI",
            "Data Science Intern",
            "Machine Learning Internship (Summer)",
            "Lead Data Scientist",
            "Engineering Manager, Machine Learning",
        ],
    )
    def test_excluded_seniority_and_intern_titles(self, title: str) -> None:
        assert is_excluded_title(title)

    @pytest.mark.parametrize(
        "title",
        [
            "Account Executive",
            "Frontend Software Engineer (React)",
            "Product Designer",
            "Customer Success Specialist",
            "Recruiter",
        ],
    )
    def test_non_target_role_family_rejected(self, title: str) -> None:
        assert not is_target_title(title)


class TestLocationAndCommuteRules:
    """Verify Mode 1 (US-Remote) and Mode 2 (<=25 min Raleigh/RTP commute) classification."""

    @pytest.mark.parametrize(
        "location_raw,expected",
        [
            ("Raleigh, NC", "Local"),
            ("Cary, North Carolina", "Local"),
            ("Morrisville, NC (RTP)", "Local"),
            ("Research Triangle Park, NC", "Local"),
            ("Durham, NC", "Local"),
            ("Apex, NC", "Local"),
            ("Garner, NC", "Local"),
            ("Chapel Hill, NC", "Local"),
            ("Fort Liberty, NC", "Local"),
            ("Fayetteville, NC", "Local"),
            ("Washington, DC", "Local"),
            ("McLean, VA", "Local"),
            ("Fort Meade, MD", "Local"),
            ("Annapolis Junction, MD", "Local"),
            ("Raleigh, NC (Hybrid)", "Hybrid"),
            ("Hybrid - Durham, NC", "Hybrid"),
            ("Hybrid - Reston, VA", "Hybrid"),
        ],
    )
    def test_local_rtp_commute_whitelist(self, location_raw: str, expected: str) -> None:
        assert classify_location(location_raw) == expected

    @pytest.mark.parametrize(
        "location_raw",
        [
            "Remote",
            "Remote - US",
            "Remote, United States",
            "Anywhere",
            "Distributed (US)",
            "Work from Home - USA",
            "WFH",
        ],
    )
    def test_us_remote_whitelist(self, location_raw: str) -> None:
        assert classify_location(location_raw) == "Remote"

    @pytest.mark.parametrize(
        "location_raw",
        [
            "Remote - Canada",
            "Remote - Ontario, Canada",
            "Remote (UK Only)",
            "Remote - EMEA",
            "Remote, APAC",
            "Remote - India",
            "Remote - LATAM",
            "Remote - Europe",
            "San Francisco, CA",
            "New York, NY",
            "Austin, TX",
            "Charlotte, NC",
            "",
            None,
        ],
    )
    def test_rejected_locations(self, location_raw: str | None) -> None:
        assert classify_location(location_raw) is None


class TestHashingAndHtmlNormalization:
    """Verify deterministic SHA-256 deduplication key and HTML stripping."""

    def test_deterministic_job_hash_normalization(self) -> None:
        # Punctuation, casing, and spacing in company/title must produce the identical hash
        h1 = compute_job_hash("Scale AI, Inc.", "Senior Data Scientist - Ads", "Remote")
        h2 = compute_job_hash("scale ai inc", "senior data scientist ads", "remote")
        assert h1 == h2
        assert len(h1) == 64

    def test_job_hash_differs_by_workplace_type(self) -> None:
        h_remote = compute_job_hash("Acme AI", "Data Scientist", "Remote")
        h_local = compute_job_hash("Acme AI", "Data Scientist", "Local")
        assert h_remote != h_local

    def test_strip_html_cleans_entities_and_scripts(self) -> None:
        raw = "&lt;div&gt;&lt;p&gt;Build &lt;b&gt;PyTorch&lt;/b&gt; models.&lt;/p&gt;&lt;script&gt;alert(1)&lt;/script&gt;&lt;/div&gt;"
        cleaned = strip_html(raw)
        assert cleaned == "Build PyTorch models."


class TestATSPollerParsers:
    """Verify Greenhouse and Lever JSON normalization with mocked HTTP responses."""

    def test_fetch_greenhouse_board_filters_and_normalizes(self) -> None:
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "id": 101,
                    "title": "Senior AI Engineer",
                    "location": {"name": "Remote - United States"},
                    "absolute_url": "https://boards.greenhouse.io/example/jobs/101",
                    "content": "<p>Experience with Python, SQL, and AWS.</p>",
                    "first_published": "2026-11-01T12:00:00Z",
                },
                {
                    "id": 102,
                    "title": "Staff AI Engineer",  # Should be knocked out by seniority
                    "location": {"name": "Remote - United States"},
                    "absolute_url": "https://boards.greenhouse.io/example/jobs/102",
                    "content": "<p>Staff role.</p>",
                },
            ]
        }
        mock_session.get.return_value = mock_resp

        matched, raw_count = fetch_greenhouse_board("example", "ExampleCorp", mock_session)
        assert raw_count == 2
        assert len(matched) == 1
        job = matched[0]
        assert job["company"] == "ExampleCorp"
        assert job["title"] == "Senior AI Engineer"
        assert job["workplace_type"] == "Remote"
        assert job["description_text"] == "Experience with Python, SQL, and AWS."
        assert len(job["job_hash"]) == 64

    def test_fetch_lever_board_filters_and_normalizes(self) -> None:
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "id": "lev-1",
                "text": "Data Scientist",
                "categories": {"location": "Raleigh, NC", "allLocations": ["Raleigh, NC"]},
                "workplaceType": "hybrid",
                "hostedUrl": "https://jobs.lever.co/example/lev-1",
                "descriptionPlain": "Raleigh hybrid data science role.",
                "salaryRange": {"min": 140000, "max": 175000},
                "createdAt": 1762000000000,
            }
        ]
        mock_session.get.return_value = mock_resp

        matched, raw_count = fetch_lever_board("example", "ExampleLever", mock_session)
        assert raw_count == 1
        assert len(matched) == 1
        job = matched[0]
        assert job["workplace_type"] == "Hybrid"
        assert job["salary_min"] == 140000
        assert job["salary_max"] == 175000

    def test_fetch_workday_cxs_and_xml_feed_parsers(self) -> None:
        mock_session = MagicMock()
        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.json.return_value = {
            "total": 2,
            "jobPostings": [
                {
                    "title": "Data Scientist",
                    "externalPath": "/job/McLean-VA/Data-Scientist_R100",
                    "locationsText": "McLean, VA",
                    "bulletFields": ["R100"],
                },
                {
                    "title": "Director of Data Science",  # Knocked out by seniority
                    "externalPath": "/job/McLean-VA/Director_R101",
                    "locationsText": "McLean, VA",
                    "bulletFields": ["R101"],
                },
            ],
        }
        mock_session.post.return_value = post_resp

        det_resp = MagicMock()
        det_resp.status_code = 200
        det_resp.json.return_value = {
            "jobPostingInfo": {
                "title": "Data Scientist",
                "location": "McLean, VA",
                "jobReqId": "R100",
                "startDate": "2026-11-05",
                "jobDescription": "<p>2+ years of Python, SQL, and Top Secret clearance for DoD analytics.</p>",
            }
        }
        mock_session.get.return_value = det_resp

        wd_matched, wd_raw = fetch_workday_cxs_board(
            tenant="bah",
            wd_instance="wd1",
            site="BAH_Jobs",
            company_name="Booz Allen Hamilton",
            session=mock_session,
            search_terms=("Data Scientist",),
        )
        assert wd_raw == 2
        assert len(wd_matched) == 1
        assert wd_matched[0]["company"] == "Booz Allen Hamilton"
        assert wd_matched[0]["source"] == "workday"
        assert wd_matched[0]["workplace_type"] == "Local"

        sample_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>AI Engineer</title>
              <link>https://careers.mitre.org/jobs/ai-eng-1</link>
              <location>McLean, VA</location>
              <description>Build DoD RAG pipelines in Python with Secret clearance.</description>
            </item>
          </channel>
        </rss>"""
        xml_matched, xml_raw = parse_xml_job_feed(sample_xml, company_name="MITRE")
        assert xml_raw == 1
        assert len(xml_matched) == 1
        assert xml_matched[0]["company"] == "MITRE"
        assert xml_matched[0]["source"] == "xml_feed"


class TestFakeRemoteAndHybridDetection:
    """Verify fake-remote out-of-state hybrid knockouts and zero false positives on legitimate remote perks."""

    def test_rejects_fake_remote_with_out_of_state_hybrid_mandate(self) -> None:
        # 1. Kyndryl Frisco TX 3-day onsite mandate disguised as 'Dallas, TX | Flexible / Remote'
        passed, wt, _, reason = evaluate_posting(
            company="Kyndryl",
            title="Data Engineer",
            location_raw="Dallas, TX | Flexible / Remote",
            description_text=(
                "This is a hybrid role requiring a minimum of 3 days per week on-site in Frisco, TX. "
                "Candidates must reside within a commutable distance."
            ),
        )
        assert passed is False
        assert wt is None
        assert "Fake remote" in reason

        # 2. OpenAI Bellevue WA hybrid work model
        passed2, _, _, _ = evaluate_posting(
            company="OpenAI",
            title="Data Engineer",
            location_raw="Seattle | Remote",
            description_text="This role is based in Bellevue. We use a hybrid work model of 3 days in the office per week.",
        )
        assert passed2 is False

    def test_preserves_legitimate_remote_with_hybrid_cloud_or_office_perks(self) -> None:
        # 1. Ramp remote role with optional in-office perks & relocation (if needed)
        passed, wt, _, _ = evaluate_posting(
            company="Ramp",
            title="Machine Learning Engineer",
            location_raw="Remote (US)",
            description_text=(
                "Build ML models in Python and PyTorch. "
                "Benefits: In-office perks: lunch, snacks, drinks. "
                "Relocation expense coverage to NYC or SF (if needed)."
            ),
        )
        assert passed is True
        assert wt == "Remote"

        # 2. Remote role mentioning 'hybrid and cloud-native environments'
        passed2, wt2, _, _ = evaluate_posting(
            company="CloudCo",
            title="Data Engineer",
            location_raw="Remote - US",
            description_text="Architect distributed systems across hybrid and cloud-native environments.",
        )
        assert passed2 is True
        assert wt2 == "Remote"

        # 3. Local Raleigh hybrid role mentioning 3 days per week on-site in Raleigh, NC
        passed3, wt3, _, _ = evaluate_posting(
            company="RTPTech",
            title="AI Engineer",
            location_raw="Raleigh, NC | Flexible / Remote",
            description_text="This is a hybrid role requiring 3 days per week on-site in Raleigh, NC.",
        )
        assert passed3 is True
        assert wt3 == "Local"

