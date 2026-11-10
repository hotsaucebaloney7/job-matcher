"""Stage 4 Phase 1 Exporter: Rich CLI summary table and daily Markdown digest generator."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import List, Optional, Sequence

from rich.console import Console
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import JobPosting
from src.scoring.filters import format_location_tag

DEFAULT_REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"
DEFAULT_MIN_SCORE = 65.0


def format_salary_range(salary_min: Optional[int], salary_max: Optional[int]) -> str:
    """Format optional min/max salary integers into a human-readable string."""
    if salary_min and salary_max:
        if salary_min == salary_max:
            return f"${salary_min:,}"
        return f"${salary_min:,} - ${salary_max:,}"
    if salary_min:
        return f"${salary_min:,}+"
    if salary_max:
        return f"Up to ${salary_max:,}"
    return "Not listed"


def format_workplace_badge(
    workplace_type: str,
    location_raw: Optional[str] = None,
) -> str:
    """Return a standardized location badge string (e.g. [Raleigh NC], [McLean VA], [Fort Meade MD], [REMOTE])."""
    if location_raw:
        tag = format_location_tag(location_raw, workplace_type)
        if tag.lower() == "remote":
            return "[REMOTE]"
        return f"[{tag}]"
    wt = (workplace_type or "Remote").strip().upper()
    return f"[{wt}]"


def get_high_scoring_new_postings(
    session: Session,
    min_score: float = DEFAULT_MIN_SCORE,
) -> List[JobPosting]:
    """Query job_postings WHERE match_score >= min_score AND status = 'new' ORDER BY match_score DESC."""
    stmt = (
        select(JobPosting)
        .where(
            JobPosting.match_score.is_not(None),
            JobPosting.match_score >= min_score,
            JobPosting.status == "new",
        )
        .order_by(JobPosting.match_score.desc(), JobPosting.date_fetched.desc())
    )
    return list(session.scalars(stmt).all())


def render_cli_table(
    postings: Sequence[JobPosting],
    console: Optional[Console] = None,
    min_score: float = DEFAULT_MIN_SCORE,
) -> None:
    """Render a Rich terminal summary table of top matching job postings."""
    out = console or Console()
    table = Table(
        title=f"Daily Job Matches (match_score >= {min_score:.0f}, status = 'new')",
        show_lines=True,
    )
    table.add_column("#", justify="right", style="dim")
    table.add_column("Score", justify="right", style="bold green")
    table.add_column("Breakdown (KW / Sem)", justify="center", style="cyan")
    table.add_column("Badge", justify="center")
    table.add_column("Exp. Tier", justify="center", style="magenta")
    table.add_column("Company", style="bold white", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Salary", justify="right", style="yellow")
    table.add_column("Matched Skill Groups", style="dim")

    for idx, row in enumerate(postings, start=1):
        badge_raw = format_workplace_badge(row.workplace_type, row.location_raw)
        badge_styled = (
            f"[green]{badge_raw}[/green]"
            if row.workplace_type == "Remote"
            else f"[yellow]{badge_raw}[/yellow]"
        )
        kw_dict = row.matched_keywords or {}
        exp_tier = kw_dict.get("experience_tier") or "Unspecified"
        matched_groups = list((kw_dict.get("matched_groups") or {}).keys())
        table.add_row(
            str(idx),
            f"{(row.match_score or 0.0):.1f}",
            f"{(row.keyword_score or 0.0):.0f} / {(row.semantic_score or 0.0):.0f}",
            badge_styled,
            exp_tier,
            row.company,
            row.title,
            format_salary_range(row.salary_min, row.salary_max),
            ", ".join(matched_groups[:6]) + ("..." if len(matched_groups) > 6 else ""),
        )

    out.print(table)


def generate_markdown_digest(
    postings: Sequence[JobPosting],
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    report_date: Optional[date] = None,
    min_score: float = DEFAULT_MIN_SCORE,
) -> Path:
    """Export a daily Markdown digest to reports/daily_matches_YYYY-MM-DD.md."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    target_date = report_date or date.today()
    report_path = reports_dir / f"daily_matches_{target_date.isoformat()}.md"

    lines: List[str] = [
        f"# Daily Job Matches Digest — {target_date.isoformat()}",
        "",
        f"- **Minimum Match Score Threshold:** `{min_score:.1f}`",
        f"- **Qualifying New Postings:** `{len(postings)}`",
        "",
    ]

    if not postings:
        lines.append("_No new postings met the minimum match score threshold today._")
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report_path

    # Quick summary table at the top of the Markdown report
    lines.extend(
        [
            "## Summary Leaderboard",
            "",
            "| Rank | Match Score | Keyword | Semantic | Exp. Tier | Mode | Company | Role | Salary |",
            "| ---: | ----------: | ------: | -------: | :-------- | :--: | :------ | :--- | :----- |",
        ]
    )
    for idx, row in enumerate(postings, start=1):
        badge = format_workplace_badge(row.workplace_type, row.location_raw)
        salary_str = format_salary_range(row.salary_min, row.salary_max)
        kw_dict = row.matched_keywords or {}
        exp_tier = kw_dict.get("experience_tier") or "Unspecified"
        lines.append(
            f"| {idx} | **{(row.match_score or 0.0):.1f}** | {(row.keyword_score or 0.0):.1f} | "
            f"{(row.semantic_score or 0.0):.1f} | `{exp_tier}` | `{badge}` | **{row.company}** | "
            f"[{row.title}]({row.url}) | {salary_str} |"
        )

    lines.extend(["", "---", "", "## Detailed Match Breakdown", ""])

    for idx, row in enumerate(postings, start=1):
        badge = format_workplace_badge(row.workplace_type, row.location_raw)
        salary_str = format_salary_range(row.salary_min, row.salary_max)
        kw_dict = row.matched_keywords or {}
        exp_tier = kw_dict.get("experience_tier") or "Unspecified"
        exp_penalty = float(kw_dict.get("experience_penalty") or 0.0)
        matched_groups = kw_dict.get("matched_groups") or {}
        missing_groups = kw_dict.get("missing_groups") or []

        penalty_note = f", Stretch Adj: `-{exp_penalty:.0f}`" if exp_penalty > 0 else ""
        lines.extend(
            [
                f"### {idx}. `{badge}` [{row.company} — {row.title}]({row.url})",
                f"- **Composite Match Score:** **{(row.match_score or 0.0):.1f} / 100** "
                f"*(Keyword Rubric: `{(row.keyword_score or 0.0):.1f}`, "
                f"Resume Embedding Similarity: `{(row.semantic_score or 0.0):.1f}`{penalty_note})*",
                f"- **Experience Required:** `{exp_tier}` | **Location:** {row.location_raw or row.workplace_type} | "
                f"**Salary Range:** {salary_str} | **Source:** `{row.source}`",
            ]
        )

        if matched_groups:
            formatted_matched = [
                f"`{group}` ({', '.join(terms)})" for group, terms in matched_groups.items()
            ]
            lines.append(f"- **Matched Skill Tags:** {', '.join(formatted_matched)}")
        else:
            lines.append("- **Matched Skill Tags:** _None_")

        if missing_groups:
            formatted_missing = ", ".join(f"`{g}`" for g in missing_groups)
            lines.append(f"- **Missing Skill Tags:** {formatted_missing}")
        else:
            lines.append("- **Missing Skill Tags:** _None (All tracked groups matched!)_")

        lines.append("")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path
