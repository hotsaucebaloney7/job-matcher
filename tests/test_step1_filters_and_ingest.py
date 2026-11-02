"""Automated QA test suite for Step 1: Knockout Filters, SHA-256 Hashing, HTML Stripping, and ATS Poller."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.ingest.ats_poller import fetch_greenhouse_board, fetch_lever_board
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
            ("Raleigh, NC (Hybrid)", "Hybrid"),
            ("Hybrid - Durham, NC", "Hybrid"),
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
