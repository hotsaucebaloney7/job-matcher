"""End-to-End Job Matcher CLI Orchestrator (Stage 1 Ingest -> Stage 2 Store -> Stage 3 Score -> Stage 4 Export)."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from rich.console import Console
from sqlalchemy import Engine, select, update

from src.db.models import JobPosting
from src.db.session import get_session_factory, init_db, upsert_job_postings
from src.exporters.markdown_digest import (
    DEFAULT_MIN_SCORE,
    DEFAULT_REPORTS_DIR,
    generate_markdown_digest,
    get_high_scoring_new_postings,
    render_cli_table,
)
from src.exporters.notion_sync import is_notion_configured, sync_to_notion
from src.ingest.adzuna_client import poll_adzuna
from src.ingest.ats_poller import DEFAULT_CONFIG_PATH, poll_all_ats
from src.ingest.external_apis import poll_jsearch_rapidapi, poll_remotive, poll_the_muse, poll_usajobs
from src.scoring.embed_scorer import DEFAULT_DATA_DIR, EmbeddingScorer, score_unscored_postings
from src.scoring.keyword_scorer import DEFAULT_KEYWORDS_PATH, KeywordScorer


def run_pipeline(
    engine: Optional[Engine] = None,
    companies_config: Path = DEFAULT_CONFIG_PATH,
    keywords_config: Path = DEFAULT_KEYWORDS_PATH,
    data_dir: Path = DEFAULT_DATA_DIR,
    resume_path: Optional[Path] = None,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    min_score: float = DEFAULT_MIN_SCORE,
    skip_ingest: bool = False,
    rescore_all: bool = False,
    report_date: Optional[date] = None,
    embed_scorer: Optional[EmbeddingScorer] = None,
    console: Optional[Console] = None,
) -> Path:
    """Execute the full Job Matcher pipeline and return the generated Markdown digest path."""
    out = console or Console()
    out.rule("[bold cyan]Job Matcher Pipeline[/bold cyan]")

    # Stage 1: Ingestion (Direct ATS Poller + Remotive + The Muse + Adzuna + JSearch)
    merged_postings: Dict[str, dict] = {}
    if not skip_ingest:
        out.print("[bold cyan]Stage 1:[/bold cyan] Polling Greenhouse, Lever, Ashby, Remotive, The Muse, Adzuna & JSearch...")
        ats_jobs, ats_stats = poll_all_ats(config_path=companies_config, console=out)
        for job in ats_jobs:
            merged_postings.setdefault(job["job_hash"], job)

        remotive_jobs, remotive_stats = poll_remotive(console=out)
        for job in remotive_jobs:
            merged_postings.setdefault(job["job_hash"], job)

        muse_jobs, muse_stats = poll_the_muse(console=out)
        for job in muse_jobs:
            merged_postings.setdefault(job["job_hash"], job)

        adzuna_jobs, adzuna_stats = poll_adzuna(console=out)
        for job in adzuna_jobs:
            merged_postings.setdefault(job["job_hash"], job)

        jsearch_jobs, jsearch_stats = poll_jsearch_rapidapi(console=out)
        for job in jsearch_jobs:
            merged_postings.setdefault(job["job_hash"], job)

        usajobs_jobs, usajobs_stats = poll_usajobs(console=out)
        for job in usajobs_jobs:
            merged_postings.setdefault(job["job_hash"], job)

        all_stats = ats_stats + remotive_stats + muse_stats + adzuna_stats + jsearch_stats + usajobs_stats
        total_raw = sum(s["raw"] for s in all_stats)
        out.print(
            f"  • Scanned [bold]{total_raw}[/bold] raw postings across all sources -> "
            f"[bold green]{len(merged_postings)}[/bold green] unique target matches passed knockout filters."
        )
    else:
        out.print("[dim]Stage 1: Skipped (--skip-ingest)[/dim]")

    # Stage 2: Normalization & MySQL 8.0 Storage
    out.print("[bold cyan]Stage 2:[/bold cyan] Syncing with MySQL 8.0 database...")
    db_engine = init_db(engine)
    SessionLocal = get_session_factory(db_engine)

    with SessionLocal() as session:
        if merged_postings:
            upserted = upsert_job_postings(session, list(merged_postings.values()))
            out.print(f"  • Upserted [bold green]{upserted}[/bold green] postings into `job_postings`.")

        if rescore_all:
            session.execute(
                update(JobPosting).values(
                    keyword_score=None,
                    semantic_score=None,
                    match_score=None,
                    matched_keywords=None,
                )
            )
            session.commit()
            out.print("  • Reset existing scores (`--rescore` enabled).")

        # Stage 3: Hybrid Keyword + Local BGE Embedding Scoring
        out.print("[bold cyan]Stage 3:[/bold cyan] Scoring unscored postings against active resume...")
        kw_scorer = KeywordScorer(config_path=keywords_config)
        active_embed_scorer = embed_scorer or EmbeddingScorer(data_dir=data_dir, resume_path=resume_path)
        if active_embed_scorer.resume_source_path:
            out.print(f"  • Active Resume: [green]{active_embed_scorer.resume_source_path}[/green]")

        scored_count = score_unscored_postings(session, kw_scorer, active_embed_scorer)
        out.print(f"  • Newly Scored Postings: [bold green]{scored_count}[/bold green]")

        # Stage 4: Output & Review Layer (Rich CLI Table + Markdown Digest + Optional Notion Sync)
        out.print(f"[bold cyan]Stage 4:[/bold cyan] Exporting matches (match_score >= {min_score:.1f})...")
        qualifying = get_high_scoring_new_postings(session, min_score=min_score)

        # If no postings meet the 65+ threshold (e.g. with placeholder resume), still show top 5 in terminal
        render_cli_table(qualifying, console=out, min_score=min_score)
        report_path = generate_markdown_digest(
            qualifying,
            reports_dir=reports_dir,
            report_date=report_date,
            min_score=min_score,
        )
        out.print(f"  • Daily Markdown Digest written to: [bold green]{report_path}[/bold green]")

        if is_notion_configured():
            synced = sync_to_notion(session, min_score=min_score, console=out)
            out.print(f"  • Synced [bold green]{synced}[/bold green] new postings to Notion.")

    out.rule("[bold green]Pipeline Complete[/bold green]")
    return report_path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Job Matcher end-to-end pipeline.")
    parser.add_argument(
        "--min-score",
        type=float,
        default=DEFAULT_MIN_SCORE,
        help="Minimum composite match_score (0-100) for the daily digest (default: 65.0).",
    )
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Re-score all stored database postings (use after updating your resume or keywords.yaml).",
    )
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Skip live ATS/Adzuna network polling and score/export existing database rows.",
    )
    parser.add_argument(
        "--resume",
        type=Path,
        default=None,
        help="Optional explicit path to a resume file (.pdf, .docx, .txt, .md).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    run_pipeline(
        min_score=args.min_score,
        skip_ingest=args.skip_ingest,
        rescore_all=args.rescore,
        resume_path=args.resume,
    )


if __name__ == "__main__":
    main()
