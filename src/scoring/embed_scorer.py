"""Local BAAI/bge-small-en-v1.5 embedding scorer, dynamic resume folder reader, and hybrid composite scorer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import numpy as np
from rich.console import Console
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import JobPosting
from src.db.session import get_session_factory, get_unscored_postings, init_db
from src.scoring.keyword_scorer import KeywordScorer

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEFAULT_RESUME_TXT = DEFAULT_DATA_DIR / "resume.txt"
DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"
SUPPORTED_RESUME_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}


def extract_text_from_file(file_path: Path) -> str:
    """Extract plain text from a .pdf, .docx, .txt, or .md resume file."""
    if not file_path.exists():
        raise FileNotFoundError(f"Resume file not found: {file_path}")

    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(file_path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(p.strip() for p in pages if p.strip())

    if suffix == ".docx":
        from docx import Document

        doc = Document(str(file_path))
        parts: List[str] = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    parts.append(row_text)
        return "\n".join(parts)

    raw_text = file_path.read_text(encoding="utf-8", errors="ignore")
    # Strip template instruction comments if reading the default placeholder
    lines = [
        line
        for line in raw_text.splitlines()
        if not line.strip().startswith("# PLACEHOLDER RESUME")
        and not line.strip().startswith("# If you place ANY")
        and not line.strip().startswith("# the Job Matcher will")
    ]
    return "\n".join(lines).strip()


def discover_resume_file(data_dir: Path = DEFAULT_DATA_DIR, explicit_path: Optional[Path] = None) -> Path:
    """Find the active resume file inside `data/`.

    Priority order:
    1. `explicit_path` if explicitly passed.
    2. Any user-dropped `.pdf`, `.docx`, `.md`, or non-placeholder `.txt` file in `data/`
       (most recently modified wins if multiple files are present).
    3. Fallback to `data/resume.txt`.
    """
    if explicit_path is not None:
        if not explicit_path.exists():
            raise FileNotFoundError(f"Explicit resume path does not exist: {explicit_path}")
        return explicit_path

    if not data_dir.exists():
        raise FileNotFoundError(f"Resume data directory not found: {data_dir}")

    candidates = [
        p
        for p in data_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_RESUME_EXTENSIONS
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No resume file (.pdf, .docx, .txt, .md) found in {data_dir}. "
            "Drop your resume into the 'data/' folder."
        )

    # Check if there are user-dropped files other than an untouched placeholder resume.txt
    non_placeholder: List[Path] = []
    for p in candidates:
        if p.name.lower() == "resume.txt":
            try:
                head = p.read_text(encoding="utf-8", errors="ignore")[:120]
                if head.startswith("# PLACEHOLDER RESUME"):
                    continue
            except OSError:
                pass
        non_placeholder.append(p)

    pool = non_placeholder if non_placeholder else candidates
    # Pick the most recently modified file
    pool.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return pool[0]


def load_resume_text(
    data_dir: Path = DEFAULT_DATA_DIR,
    explicit_path: Optional[Path] = None,
) -> Tuple[str, Path]:
    """Discover and extract text from the user's resume in `data/`.

    Returns:
        Tuple of (resume_plain_text, resolved_file_path)
    """
    resolved_path = discover_resume_file(data_dir=data_dir, explicit_path=explicit_path)
    text = extract_text_from_file(resolved_path)
    if not text.strip():
        raise ValueError(f"Resume file {resolved_path} is empty after text extraction.")
    return text, resolved_path


def compute_composite_score(
    keyword_score: float,
    semantic_score: float,
    experience_penalty: float = 0.0,
    is_snippet: bool = False,
) -> float:
    """Compute Stage 3.4 weighted composite match score (50% keyword + 50% semantic - stretch penalty).

    For truncated aggregator snippets (<= 600 chars, e.g., Adzuna 500-char summaries),
    applies snippet-length normalization so local aggregator matches compete fairly
    against full-length ATS descriptions.
    """
    kw = float(keyword_score)
    sem = float(semantic_score)
    if is_snippet:
        boosted_kw = min(100.0, kw * 1.45)
        composite = 0.40 * boosted_kw + 0.60 * sem - float(experience_penalty)
    else:
        composite = 0.50 * kw + 0.50 * sem - float(experience_penalty)
    return round(min(max(composite, 0.0), 100.0), 2)


class EmbeddingScorer:
    """Encodes the user's resume and job descriptions using BAAI/bge-small-en-v1.5."""

    def __init__(
        self,
        resume_text: Optional[str] = None,
        data_dir: Path = DEFAULT_DATA_DIR,
        resume_path: Optional[Path] = None,
        model_name: str = DEFAULT_MODEL_NAME,
        model: Optional[Any] = None,
    ) -> None:
        if resume_text is not None:
            self.resume_text = resume_text
            self.resume_source_path: Optional[Path] = resume_path
        else:
            self.resume_text, self.resume_source_path = load_resume_text(
                data_dir=data_dir,
                explicit_path=resume_path,
            )

        self.model_name = model_name
        self._model = model
        self._resume_embedding: Optional[np.ndarray] = None

    @property
    def model(self) -> Any:
        """Lazily load SentenceTransformer model on first use."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    @property
    def resume_embedding(self) -> np.ndarray:
        """Return L2-normalized embedding vector for the active resume."""
        if self._resume_embedding is None:
            vec = self.model.encode(
                self.resume_text,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            self._resume_embedding = np.asarray(vec, dtype=np.float32)
        return self._resume_embedding

    def score(self, description_text: str) -> float:
        """Compute cosine similarity between resume and description_text scaled to 0–100."""
        if not description_text or not description_text.strip():
            return 0.0
        desc_vec = self.model.encode(
            description_text,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        desc_arr = np.asarray(desc_vec, dtype=np.float32)
        cos_sim = float(np.dot(self.resume_embedding, desc_arr))
        scaled = min(max(cos_sim * 100.0, 0.0), 100.0)
        return round(scaled, 2)

    def score_batch(self, descriptions: Sequence[str]) -> List[float]:
        """Encode a batch of job descriptions and return 0–100 semantic similarity scores."""
        if not descriptions:
            return []
        cleaned = [d if (d and d.strip()) else " " for d in descriptions]
        vecs = self.model.encode(
            cleaned,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        mat = np.asarray(vecs, dtype=np.float32)
        sims = np.dot(mat, self.resume_embedding)
        scores: List[float] = []
        for idx, orig in enumerate(descriptions):
            if not orig or not orig.strip():
                scores.append(0.0)
            else:
                val = min(max(float(sims[idx]) * 100.0, 0.0), 100.0)
                scores.append(round(val, 2))
        return scores


def score_unscored_postings(
    session: Session,
    keyword_scorer: Optional[KeywordScorer] = None,
    embed_scorer: Optional[EmbeddingScorer] = None,
) -> int:
    """Score all rows in job_postings where match_score IS NULL and persist results."""
    unscored = get_unscored_postings(session)
    if not unscored:
        return 0

    kw_scorer = keyword_scorer or KeywordScorer()
    emb_scorer = embed_scorer or EmbeddingScorer()

    descriptions = [
        f"{row.title or ''}\n{row.description_text or ''}".strip() for row in unscored
    ]
    semantic_scores = emb_scorer.score_batch(descriptions)

    for row, sem_score in zip(unscored, semantic_scores):
        desc_body = row.description_text or ""
        combined_text = f"{row.title or ''}\n{desc_body}".strip()
        kw_score, breakdown = kw_scorer.score(combined_text, title=row.title or "")
        row.keyword_score = kw_score
        row.semantic_score = sem_score
        row.matched_keywords = breakdown

        if breakdown.get("experience_knockout"):
            row.match_score = 0.0
            row.status = "rejected"
        else:
            penalty = float(breakdown.get("experience_penalty") or 0.0)
            is_snippet = row.source == "adzuna" and 0 < len(desc_body) <= 600
            row.match_score = compute_composite_score(
                kw_score,
                sem_score,
                experience_penalty=penalty,
                is_snippet=is_snippet,
            )
            if row.status == "rejected":
                row.status = "new"

    session.commit()
    return len(unscored)


def main() -> None:
    """CLI entrypoint to score unscored DB postings and display a hybrid score leaderboard."""
    console = Console()
    console.print("[bold cyan]Stage 3: Running Hybrid Keyword + Local Embedding Scorer...[/bold cyan]")

    engine = init_db()
    SessionLocal = get_session_factory(engine)

    kw_scorer = KeywordScorer()
    emb_scorer = EmbeddingScorer()
    console.print(f"[green]Active Resume Loaded:[/green] {emb_scorer.resume_source_path}")

    with SessionLocal() as session:
        newly_scored = score_unscored_postings(session, kw_scorer, emb_scorer)
        console.print(f"[bold green]Newly Scored Postings:[/bold green] {newly_scored}")

        stmt = select(JobPosting).where(JobPosting.match_score.is_not(None)).order_by(JobPosting.match_score.desc())
        rows = list(session.scalars(stmt).all())

        table = Table(title="Stage 3 Hybrid Scoring Leaderboard", show_lines=True)
        table.add_column("Company", style="bold cyan", no_wrap=True)
        table.add_column("Title", style="white")
        table.add_column("Keyword (50%)", justify="right")
        table.add_column("Semantic (50%)", justify="right")
        table.add_column("Match Score", justify="right", style="bold green")
        table.add_column("Matched Groups", style="dim")

        for row in rows[:15]:
            matched_groups = list(((row.matched_keywords or {}).get("matched_groups") or {}).keys())
            table.add_row(
                row.company,
                row.title,
                f"{row.keyword_score:.1f}",
                f"{row.semantic_score:.1f}",
                f"{row.match_score:.1f}",
                ", ".join(matched_groups[:5]) + ("..." if len(matched_groups) > 5 else ""),
            )
        console.print(table)


if __name__ == "__main__":
    main()
