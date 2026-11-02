"""Automated QA test suite for Step 3: Keyword Rubric, Resume Folder Discovery, and Hybrid Embedding Scorer."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, List, Sequence

import numpy as np
import pytest

from src.db.models import JobPosting
from src.db.session import get_engine, get_session_factory, init_db, upsert_job_postings
from src.scoring.embed_scorer import (
    EmbeddingScorer,
    compute_composite_score,
    discover_resume_file,
    load_resume_text,
    score_unscored_postings,
)
from src.scoring.filters import compute_job_hash, evaluate_posting, extract_min_years_experience
from src.scoring.keyword_scorer import KeywordScorer


class DummyEncoder:
    """Fast deterministic 3D unit-vector encoder for unit testing EmbeddingScorer."""

    def encode(
        self,
        sentences: str | Sequence[str],
        normalize_embeddings: bool = True,
        show_progress_bar: bool = False,
    ) -> np.ndarray:
        def _vec(text: str) -> np.ndarray:
            t = text.lower()
            v = np.array(
                [
                    1.0 if "python" in t else 0.1,
                    1.0 if "pytorch" in t else 0.1,
                    1.0 if "rag" in t else 0.1,
                ],
                dtype=np.float32,
            )
            norm = np.linalg.norm(v)
            return v / norm if (normalize_embeddings and norm > 0) else v

        if isinstance(sentences, str):
            return _vec(sentences)
        return np.stack([_vec(s) for s in sentences], axis=0)


class TestKeywordScorerRubric:
    """Verify Tier 1–3 scoring weights, per-group deduplication, tier caps, and word boundaries."""

    def test_single_group_does_not_double_count_multiple_terms(self) -> None:
        scorer = KeywordScorer()
        # All three belong to Tier 1 -> Python Ecosystem (+15 pts max once)
        score, breakdown = scorer.score("We use Python, Pandas, and NumPy every day.")
        assert score == 15.0
        assert breakdown["tier_scores"]["tier_1"] == 15.0
        assert set(breakdown["matched_groups"]["Python Ecosystem"]) == {"python", "pandas", "numpy"}

    def test_tier_caps_and_perfect_100_score(self) -> None:
        scorer = KeywordScorer()
        full_text = (
            # Tier 1: all 4 groups -> 4 * 15 = 60 (cap 60)
            "Python SQL AWS Machine Learning. "
            # Tier 2: all 4 groups -> 4 * 10 = 40 -> capped at 30!
            "Airflow LLM RAG PyTorch FastAPI. "
            # Tier 3: all 3 groups -> 3 * 5 = 15 -> capped at 10!
            "M.S. Ph.D. Docker CI/CD A/B testing Causal Inference."
        )
        score, breakdown = scorer.score(full_text)
        assert breakdown["tier_scores"]["tier_1"] == 60.0
        assert breakdown["tier_scores"]["tier_2"] == 30.0
        assert breakdown["tier_scores"]["tier_3"] == 10.0
        assert score == 100.0
        assert breakdown["missing_groups"] == []

    def test_word_boundary_prevents_substring_false_positives(self) -> None:
        scorer = KeywordScorer()
        # 'leverage'/'average' contain 'rag'; 'digital' contains 'git'; 'cloud' should not match 'cloudy'
        score, breakdown = scorer.score("We leverage digital averages on a cloudy day.")
        assert score == 0.0
        assert breakdown["matched_groups"] == {}


class TestResumeFolderDiscovery:
    """Verify automatic discovery and extraction of user resumes dropped into data/."""

    def test_user_dropped_resume_overrides_placeholder(self, tmp_path: Path) -> None:
        placeholder = tmp_path / "resume.txt"
        placeholder.write_text("# PLACEHOLDER RESUME — Drop your actual resume...\nPlaceholder text", encoding="utf-8")

        user_md = tmp_path / "My_2026_Resume.md"
        user_md.write_text("# Jane Doe\nSenior AI Engineer with Python, PyTorch, and AWS.", encoding="utf-8")

        resolved = discover_resume_file(data_dir=tmp_path)
        assert resolved == user_md

        text, path = load_resume_text(data_dir=tmp_path)
        assert path == user_md
        assert "Jane Doe" in text

    def test_docx_resume_extraction(self, tmp_path: Path) -> None:
        from docx import Document

        docx_path = tmp_path / "Candidate_Resume.docx"
        doc = Document()
        doc.add_paragraph("John Smith - Data Scientist")
        doc.add_paragraph("Skills: Python, SQL, PyTorch, RAG, AWS")
        doc.save(str(docx_path))

        text, path = load_resume_text(data_dir=tmp_path)
        assert path == docx_path
        assert "John Smith - Data Scientist" in text
        assert "PyTorch" in text


class TestHybridEmbeddingAndCompositeScoring:
    """Verify cosine similarity scaling, 50/50 composite formula, and DB scoring pipeline."""

    def test_embedding_scorer_and_composite_math(self) -> None:
        emb_scorer = EmbeddingScorer(
            resume_text="Python PyTorch RAG Engineer",
            model=DummyEncoder(),
        )
        identical_score = emb_scorer.score("Building Python PyTorch RAG systems")
        assert identical_score == 100.0

        composite = compute_composite_score(keyword_score=80.0, semantic_score=90.0)
        assert composite == 85.0

    def test_score_unscored_postings_updates_database(self) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        SessionLocal = get_session_factory(engine)

        with SessionLocal() as session:
            job_hash = compute_job_hash("Anthropic", "AI Engineer", "Remote")
            upsert_job_postings(
                session,
                [
                    {
                        "job_hash": job_hash,
                        "source": "greenhouse",
                        "external_id": "1",
                        "company": "Anthropic",
                        "title": "AI Engineer",
                        "location_raw": "Remote - US",
                        "workplace_type": "Remote",
                        "url": "https://boards.greenhouse.io/anthropic/jobs/1",
                        "description_text": "Python, SQL, AWS, Machine Learning, PyTorch, RAG, Docker.",
                        "date_posted": datetime(2026, 11, 1, 12, 0, 0),
                    }
                ],
            )

            kw_scorer = KeywordScorer()
            emb_scorer = EmbeddingScorer(
                resume_text="Python PyTorch RAG Engineer",
                model=DummyEncoder(),
            )
            scored_count = score_unscored_postings(session, kw_scorer, emb_scorer)
            assert scored_count == 1

            row = session.get(JobPosting, job_hash)
            assert row is not None
            assert row.keyword_score == 85.0  # Tier 1: 60 + Tier 2: 20 + Tier 3: 5 = 85
            assert row.semantic_score == 100.0
            assert row.match_score == 92.5
            assert "Python Ecosystem" in (row.matched_keywords or {}).get("matched_groups", {})


class TestExperienceExtractionAndTiers:
    """Verify 1-3 yr Primary, 3-5 yr Stretch, and 6+ yr Knockout experience rules."""

    @pytest.mark.parametrize(
        "text,expected_years",
        [
            ("Requires 2+ years of experience in data science or machine learning.", 2),
            ("1-3 years of professional experience building Python pipelines.", 1),
            ("Minimum of 4 years of experience in applied AI.", 4),
            ("5+ years of experience with a BS, or 3+ years of experience with an MS/PhD.", 3),
            ("8+ years of experience in production ML systems.", 8),
            ("For over 25 years, our company has built great products.", None),
            ("Must be at least 18 years of age.", None),
        ],
    )
    def test_extract_min_years_experience(self, text: str, expected_years: int | None) -> None:
        assert extract_min_years_experience(text) == expected_years

    def test_primary_vs_stretch_vs_knockout_tiers(self) -> None:
        scorer = KeywordScorer()

        # 2+ years -> Primary (1-3 yrs), 0 penalty
        _, primary_info = scorer.score("2+ years of experience with Python and SQL.", title="Data Scientist")
        assert primary_info["min_years_required"] == 2
        assert primary_info["experience_tier"] == "2+ yrs (Primary 1-3)"
        assert primary_info["experience_penalty"] == 0.0
        assert primary_info["experience_knockout"] is False

        # 5+ years -> Stretch (3-5 yrs), 3.0 pt penalty
        _, stretch_info = scorer.score("5+ years of experience with Python and SQL.", title="Senior Data Scientist")
        assert stretch_info["min_years_required"] == 5
        assert stretch_info["experience_tier"] == "5+ yrs (Stretch 3-5)"
        assert stretch_info["experience_penalty"] == 3.0
        assert stretch_info["experience_knockout"] is False

        # 7+ years -> Hard knockout (>5 yrs)
        _, ko_info = scorer.score("7+ years of experience in machine learning.", title="Machine Learning Engineer")
        assert ko_info["min_years_required"] == 7
        assert ko_info["experience_knockout"] is True

        passed, _, _, reason = evaluate_posting(
            company="Acme",
            title="Machine Learning Engineer",
            location_raw="Remote - US",
            description_text="Requires 7+ years of experience in machine learning.",
        )
        assert not passed
        assert "7+ years" in reason

