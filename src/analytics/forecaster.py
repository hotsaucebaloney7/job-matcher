"""Empirical Bayes + Monte Carlo Time-to-Offer Forecaster & Strategy Health Diagnostics.

Reads `job_postings` and `application_events` from MySQL, accounts for right-censored
in-flight applications, simulates 10,000 hiring trajectories against a target written-offer
deadline (default: Feb 14, 2027), and flags stage/sector bottlenecks.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from rich.console import Console
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import ApplicationEvent, JobPosting
from src.db.session import get_session_factory, init_db
from src.exporters.notion_sync import (
    is_notion_configured,
    sync_notion_board_transitions,
    update_notion_board_counts_banner,
)
from src.scoring.filters import classify_priority_tier, classify_sector

DEFAULT_TARGET_OFFER_DATE = date(2027, 2, 14)
DEFAULT_FORECAST_REPORT = Path(__file__).resolve().parents[2] / "reports" / "offer_forecast.md"

# Empirical Bayes Beta(alpha, beta) priors for P(Interview | Applied) by Priority tier
TIER_INTERVIEW_PRIORS: Dict[str, Tuple[float, float]] = {
    "Tier 1": (2.4, 17.6),  # Prior mean: 12.0%
    "Tier 2": (1.0, 19.0),  # Prior mean:  5.0%
    "Tier 3": (0.4, 19.6),  # Prior mean:  2.0%
}

# Prior for P(Offer | Interview)
OFFER_GIVEN_INTERVIEW_PRIOR: Tuple[float, float] = (3.0, 7.0)  # Prior mean: 30.0%

# Right-censoring threshold: unresponded applications older than 30 days count as resolved non-responses
GHOST_RESOLUTION_DAYS = 30


@dataclass
class InFlightApp:
    company: str
    title: str
    stage: str  # "Applied" or "Interviewing"
    priority_tier: str
    sector: str
    source: str
    days_elapsed: int


@dataclass
class FunnelSnapshot:
    reference_date: date
    target_date: date
    stage_counts: Dict[str, int]
    in_flight_apps: List[InFlightApp]
    tier_resolved: Dict[str, Dict[str, int]]
    sector_stats: Dict[str, Dict[str, int]]
    exp_stats: Dict[str, Dict[str, int]]
    source_stats: Dict[str, Dict[str, int]]
    weekly_pace_14d: float
    weekly_pace_28d: float
    fast_reject_count: int
    total_resolved_applied: int
    total_interviews: int
    total_offers: int


@dataclass
class ForecastResult:
    reference_date: date
    target_date: date
    weekly_pace_used: float
    p25_date: Optional[date]
    p50_date: Optional[date]
    p80_date: Optional[date]
    prob_by_target: float
    recommended_weekly_pace: float
    pace_sensitivity: List[Dict[str, Any]]
    posterior_rates: Dict[str, float]
    offer_given_interview_rate: float
    alerts: List[str] = field(default_factory=list)
    snapshot: Optional[FunnelSnapshot] = None


def build_funnel_snapshot(
    session: Session,
    reference_date: Optional[date] = None,
    target_date: date = DEFAULT_TARGET_OFFER_DATE,
) -> FunnelSnapshot:
    """Aggregate current board state and historical transition events into a forecasting snapshot."""
    ref_day = reference_date or date.today()
    ref_dt = datetime(ref_day.year, ref_day.month, ref_day.day, 23, 59, 59)

    rows = list(session.scalars(select(JobPosting)).all())
    events = list(session.scalars(select(ApplicationEvent).order_by(ApplicationEvent.event_at.asc())).all())

    # Track per-page event history to detect fast (<48h) rejections and true interview milestones
    events_by_page: Dict[str, List[ApplicationEvent]] = defaultdict(list)
    for ev in events:
        events_by_page[ev.notion_page_id].append(ev)

    stage_counts: Dict[str, int] = defaultdict(int)
    in_flight_apps: List[InFlightApp] = []
    tier_resolved: Dict[str, Dict[str, int]] = {
        "Tier 1": {"success": 0, "fail": 0, "in_flight": 0},
        "Tier 2": {"success": 0, "fail": 0, "in_flight": 0},
        "Tier 3": {"success": 0, "fail": 0, "in_flight": 0},
    }
    sector_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"applied": 0, "interview": 0, "declined": 0, "offer": 0})
    exp_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"applied": 0, "interview": 0, "declined": 0})
    source_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"applied": 0, "interview": 0, "declined": 0})

    applied_dates: List[date] = []
    fast_reject_count = 0
    total_interviews = 0
    total_offers = 0

    for r in rows:
        st = (r.notion_status or "").strip()
        if not st:
            continue
        stage_counts[st] += 1

        mk = r.matched_keywords if isinstance(r.matched_keywords, dict) else {}
        exp_tier = str(mk.get("experience_tier") or "Unspecified (Target)")
        exp_bucket = "Stretch (4-5 / GS-11)" if ("stretch" in exp_tier.lower() or "4+" in exp_tier or "5+" in exp_tier) else "Primary (1-3 / GS-9)"
        prio = classify_priority_tier(r.source, r.company, r.title, r.match_score, r.date_posted, experience_tier=exp_tier)
        sec = r.sector or classify_sector(r.company, r.title, r.description_text, r.source)
        src_bucket = (
            "USAJOBS"
            if r.source == "usajobs"
            else ("Direct ATS" if r.source in {"workday", "greenhouse", "lever", "ashby", "smartrecruiters", "oracle_hcm"} else "Aggregator")
        )

        # Check if this posting was ever applied to
        page_evts = events_by_page.get(r.notion_page_id or "", [])
        ever_applied = st in {"Applied", "Follow Up Needed", "Interviewing", "Offer", "Declined / Rejected"} or any(
            e.to_status == "Applied" for e in page_evts
        )
        ever_interviewed = st in {"Interviewing", "Offer"} or any(e.to_status == "Interviewing" for e in page_evts)

        if not ever_applied:
            continue

        app_dt = r.date_applied or next((e.event_at for e in page_evts if e.to_status == "Applied"), None) or r.date_fetched
        app_day = app_dt.date() if isinstance(app_dt, datetime) else ref_day
        applied_dates.append(app_day)
        days_since_applied = max(0, (ref_day - app_day).days)

        sector_stats[sec]["applied"] += 1
        exp_stats[exp_bucket]["applied"] += 1
        source_stats[src_bucket]["applied"] += 1

        if st == "Offer":
            total_offers += 1
            total_interviews += 1
            tier_resolved[prio]["success"] += 1
            sector_stats[sec]["offer"] += 1
            sector_stats[sec]["interview"] += 1
            exp_stats[exp_bucket]["interview"] += 1
            source_stats[src_bucket]["interview"] += 1
        elif st == "Interviewing":
            total_interviews += 1
            tier_resolved[prio]["success"] += 1
            sector_stats[sec]["interview"] += 1
            exp_stats[exp_bucket]["interview"] += 1
            source_stats[src_bucket]["interview"] += 1
            in_flight_apps.append(
                InFlightApp(
                    company=r.company,
                    title=r.title,
                    stage="Interviewing",
                    priority_tier=prio,
                    sector=sec,
                    source=r.source,
                    days_elapsed=max(0, (ref_day - (r.date_outcome.date() if r.date_outcome else app_day)).days),
                )
            )
        elif st == "Declined / Rejected":
            sector_stats[sec]["declined"] += 1
            exp_stats[exp_bucket]["declined"] += 1
            source_stats[src_bucket]["declined"] += 1
            if ever_interviewed:
                total_interviews += 1
                tier_resolved[prio]["success"] += 1
                sector_stats[sec]["interview"] += 1
            else:
                tier_resolved[prio]["fail"] += 1
                if r.date_outcome and app_dt:
                    lag_hours = (r.date_outcome - app_dt).total_seconds() / 3600.0
                    if 0 <= lag_hours <= 48:
                        fast_reject_count += 1
        elif st in {"Applied", "Follow Up Needed"}:
            if days_since_applied > GHOST_RESOLUTION_DAYS:
                tier_resolved[prio]["fail"] += 1
            else:
                tier_resolved[prio]["in_flight"] += 1
                in_flight_apps.append(
                    InFlightApp(
                        company=r.company,
                        title=r.title,
                        stage="Applied",
                        priority_tier=prio,
                        sector=sec,
                        source=r.source,
                        days_elapsed=days_since_applied,
                    )
                )

    apps_last_14d = sum(1 for d in applied_dates if 0 <= (ref_day - d).days <= 14)
    apps_last_28d = sum(1 for d in applied_dates if 0 <= (ref_day - d).days <= 28)
    pace_14d = round(apps_last_14d / 2.0, 1)
    pace_28d = round(apps_last_28d / 4.0, 1)
    total_resolved = sum(v["success"] + v["fail"] for v in tier_resolved.values())

    return FunnelSnapshot(
        reference_date=ref_day,
        target_date=target_date,
        stage_counts=dict(stage_counts),
        in_flight_apps=in_flight_apps,
        tier_resolved=tier_resolved,
        sector_stats=dict(sector_stats),
        exp_stats=dict(exp_stats),
        source_stats=dict(source_stats),
        weekly_pace_14d=pace_14d,
        weekly_pace_28d=pace_28d,
        fast_reject_count=fast_reject_count,
        total_resolved_applied=total_resolved,
        total_interviews=total_interviews,
        total_offers=total_offers,
    )


def _draw_lognormal_days(rng: np.random.Generator, median_days: float, sigma: float, size: int) -> np.ndarray:
    return rng.lognormal(mean=np.log(max(1.0, median_days)), sigma=sigma, size=size)


def _simulate_days_to_first_offer(
    snapshot: FunnelSnapshot,
    weekly_pace: float,
    posterior_int: Dict[str, float],
    p_offer_given_int: float,
    horizon_days: int = 240,
    n_sims: int = 10000,
    seed: int = 42,
) -> np.ndarray:
    """Return an array of length n_sims containing days until first written offer (np.inf if none within horizon)."""
    rng = np.random.default_rng(seed)
    first_offer_days = np.full(n_sims, np.inf, dtype=float)

    # 1. Simulate existing in-flight Interviewing & Applied cards
    for app in snapshot.in_flight_apps:
        is_fed = app.source == "usajobs"
        if app.stage == "Interviewing":
            wins = rng.random(n_sims) < p_offer_given_int
            rem_days = np.maximum(3.0, _draw_lognormal_days(rng, 30.0 if is_fed else 18.0, 0.45, n_sims) - app.days_elapsed)
            candidate_days = np.where(wins, rem_days, np.inf)
            first_offer_days = np.minimum(first_offer_days, candidate_days)
        else:
            p_int = posterior_int.get(app.priority_tier, 0.05)
            # Survival conditioning: probability of still hearing back decays slightly with days elapsed
            survival_factor = max(0.25, 1.0 - (app.days_elapsed / 45.0))
            gets_offer = rng.random(n_sims) < (p_int * survival_factor * p_offer_given_int)
            screen_lag = np.maximum(2.0, _draw_lognormal_days(rng, 35.0 if is_fed else 16.0, 0.5, n_sims) - app.days_elapsed)
            offer_lag = _draw_lognormal_days(rng, 30.0 if is_fed else 21.0, 0.4, n_sims)
            candidate_days = np.where(gets_offer, screen_lag + offer_lag, np.inf)
            first_offer_days = np.minimum(first_offer_days, candidate_days)

    # 2. Simulate future weekly applications over horizon_days (assuming 60% Tier 1, 35% Tier 2, 5% Tier 3 mix)
    if weekly_pace > 0:
        blended_p_int = (
            0.60 * posterior_int["Tier 1"]
            + 0.35 * posterior_int["Tier 2"]
            + 0.05 * posterior_int["Tier 3"]
        )
        p_offer_per_app = blended_p_int * p_offer_given_int
        n_weeks = max(1, horizon_days // 7)
        apps_per_week = max(1, int(round(weekly_pace)))

        for wk in range(n_weeks):
            submit_day = wk * 7.0 + 3.5
            # Probability at least one application submitted this week eventually converts to an offer
            week_offer_prob = 1.0 - (1.0 - p_offer_per_app) ** apps_per_week
            wins = rng.random(n_sims) < week_offer_prob
            if np.any(wins):
                total_lag = (
                    _draw_lognormal_days(rng, 18.0, 0.5, n_sims)
                    + _draw_lognormal_days(rng, 21.0, 0.4, n_sims)
                )
                candidate_days = np.where(wins, submit_day + total_lag, np.inf)
                first_offer_days = np.minimum(first_offer_days, candidate_days)

    return first_offer_days


def run_offer_forecast(
    session: Session,
    reference_date: Optional[date] = None,
    target_date: date = DEFAULT_TARGET_OFFER_DATE,
    override_weekly_pace: Optional[float] = None,
    n_sims: int = 10000,
    seed: int = 42,
) -> ForecastResult:
    """Run Empirical Bayes + Monte Carlo time-to-offer forecast and generate strategy diagnostics."""
    snap = build_funnel_snapshot(session, reference_date=reference_date, target_date=target_date)

    # Compute Bayesian posterior mean conversion rates per Priority tier
    posterior_int: Dict[str, float] = {}
    for tier, (a0, b0) in TIER_INTERVIEW_PRIORS.items():
        succ = snap.tier_resolved[tier]["success"]
        fail = snap.tier_resolved[tier]["fail"]
        posterior_int[tier] = round((a0 + succ) / (a0 + b0 + succ + fail), 4)

    o_a0, o_b0 = OFFER_GIVEN_INTERVIEW_PRIOR
    resolved_interviews = max(0, snap.total_interviews - sum(1 for a in snap.in_flight_apps if a.stage == "Interviewing"))
    p_offer_given_int = round((o_a0 + snap.total_offers) / (o_a0 + o_b0 + resolved_interviews), 4)

    pace_used = (
        override_weekly_pace
        if override_weekly_pace is not None
        else max(snap.weekly_pace_14d, snap.weekly_pace_28d, 8.0)
    )
    days_to_target = max(1, (target_date - snap.reference_date).days)

    sim_days = _simulate_days_to_first_offer(
        snap,
        weekly_pace=pace_used,
        posterior_int=posterior_int,
        p_offer_given_int=p_offer_given_int,
        n_sims=n_sims,
        seed=seed,
    )
    finite_days = sim_days[np.isfinite(sim_days)]
    prob_by_target = round(float(np.mean(sim_days <= days_to_target)) * 100.0, 1)

    def _pct_date(q: float) -> Optional[date]:
        if len(finite_days) == 0:
            return None
        val = float(np.percentile(sim_days, q))
        if not np.isfinite(val):
            return None
        return snap.reference_date + timedelta(days=int(round(val)))

    p25 = _pct_date(25)
    p50 = _pct_date(50)
    p80 = _pct_date(80)

    # Sensitivity analysis across weekly application paces (5, 10, 15, 20 apps/week)
    sensitivity: List[Dict[str, Any]] = []
    rec_pace = 20.0
    found_rec = False
    for test_pace in (5, 8, 10, 12, 15, 20):
        s_days = _simulate_days_to_first_offer(
            snap,
            weekly_pace=float(test_pace),
            posterior_int=posterior_int,
            p_offer_given_int=p_offer_given_int,
            n_sims=4000,
            seed=seed,
        )
        p_hit = round(float(np.mean(s_days <= days_to_target)) * 100.0, 1)
        med_val = float(np.percentile(s_days, 50))
        med_dt = (snap.reference_date + timedelta(days=int(round(med_val)))) if np.isfinite(med_val) else None
        if test_pace in (5, 10, 15, 20):
            sensitivity.append({"weekly_pace": test_pace, "prob_by_target": p_hit, "median_date": med_dt})
        if not found_rec and p_hit >= 80.0:
            rec_pace = float(test_pace)
            found_rec = True

    # Strategy Health & Bottleneck Diagnostics
    alerts: List[str] = []
    if prob_by_target < 75.0:
        alerts.append(
            f"Pace Alert: Current pace ({pace_used:.1f} apps/wk) yields {prob_by_target:.0f}% chance of a written offer "
            f"by {target_date.isoformat()}. Targeting {rec_pace:.0f} Tier 1/2 apps/wk brings this above 80%."
        )
    if snap.total_resolved_applied >= 20:
        obs_int_rate = snap.total_interviews / max(1, snap.total_resolved_applied)
        if obs_int_rate < 0.05:
            alerts.append(
                f"Top-of-Funnel Alert: Observed interview rate is {obs_int_rate*100:.1f}% across "
                f"{snap.total_resolved_applied} resolved applications (< 5%). Consider reviewing resume framing, "
                "clearance requirements, or shifting volume toward higher-converting sectors."
            )
    if snap.fast_reject_count >= 3:
        alerts.append(
            f"Fast-Reject Alert: {snap.fast_reject_count} applications were rejected within 48 hours "
            "(likely a hard Workday knockout question such as Day-1 active TS/SCI w/ poly or strict YoE)."
        )
    if snap.total_interviews >= 4 and snap.total_offers == 0:
        alerts.append(
            f"Late-Stage Alert: {snap.total_interviews} interviews reached with 0 offers yet — "
            "shift focus toward technical case & behavioral interview prep."
        )
    if not alerts:
        alerts.append("Pipeline Healthy: No statistical bottlenecks detected; keep executing Tier 1 applications.")

    return ForecastResult(
        reference_date=snap.reference_date,
        target_date=target_date,
        weekly_pace_used=pace_used,
        p25_date=p25,
        p50_date=p50,
        p80_date=p80,
        prob_by_target=prob_by_target,
        recommended_weekly_pace=rec_pace,
        pace_sensitivity=sensitivity,
        posterior_rates=posterior_int,
        offer_given_interview_rate=p_offer_given_int,
        alerts=alerts,
        snapshot=snap,
    )


def format_banner_summary(result: ForecastResult) -> str:
    """Format a compact 1-line forecast summary for the Notion top callout banner."""
    med_str = result.p50_date.strftime("%b %d, %Y") if result.p50_date else "TBD"
    return (
        f"Median Written Offer: {med_str}  |  "
        f"{result.prob_by_target:.0f}% prob by {result.target_date.strftime('%b %d, %Y')} "
        f"(at {result.weekly_pace_used:.0f} apps/wk; >=80% target pace: {result.recommended_weekly_pace:.0f}/wk)"
    )


def write_forecast_markdown(
    result: ForecastResult,
    output_path: Path = DEFAULT_FORECAST_REPORT,
) -> Path:
    """Write a full Markdown offer forecast & strategy bottleneck report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    snap = result.snapshot
    fmt_d = lambda d: d.strftime("%b %d, %Y") if d else "Beyond Horizon"

    lines: List[str] = [
        f"# 📈 Job Offer Forecast & Funnel Diagnostics — {result.reference_date.isoformat()}",
        "",
        f"- **Target Written Offer Deadline:** `{result.target_date.strftime('%B %d, %Y')}`",
        f"- **Probability of Written Offer by Deadline:** **`{result.prob_by_target:.1f}%`** (at `{result.weekly_pace_used:.1f}` apps/week)",
        f"- **Expected Written Offer Window:** Optimistic (P25): `{fmt_d(result.p25_date)}` | **Median (P50): `{fmt_d(result.p50_date)}`** | Conservative (P80): `{fmt_d(result.p80_date)}`",
        f"- **Recommended Weekly Pace (for >=80% by deadline):** **`{result.recommended_weekly_pace:.0f}` Tier 1/2 applications/week**",
        "",
        "## Strategy Health & Bottleneck Alerts",
    ]
    for alert in result.alerts:
        lines.append(f"- {alert}")

    lines.extend(
        [
            "",
            "## Weekly Pace Sensitivity (Monte Carlo — 4,000 Trials Each)",
            "",
            "| Weekly Pace | Median Offer Date | Prob. of Offer by Feb 14, 2027 |",
            "| ----------: | :---------------- | -----------------------------: |",
        ]
    )
    for row in result.pace_sensitivity:
        lines.append(
            f"| {row['weekly_pace']} apps/wk | {fmt_d(row['median_date'])} | **{row['prob_by_target']:.1f}%** |"
        )

    if snap is not None:
        lines.extend(
            [
                "",
                "## Sector ROI Breakdown",
                "",
                "| Sector | Applied | Interviewing / Reached | Declined | Offers |",
                "| :----- | ------: | ---------------------: | -------: | -----: |",
            ]
        )
        for sec, stats in sorted(snap.sector_stats.items(), key=lambda kv: -kv[1]["applied"]):
            lines.append(
                f"| {sec} | {stats['applied']} | {stats['interview']} | {stats['declined']} | {stats['offer']} |"
            )

        lines.extend(
            [
                "",
                "## Bayesian Posterior Conversion Rates (Right-Censored)",
                "",
                f"- **Tier 1 `P(Interview | Applied)`:** `{result.posterior_rates['Tier 1']*100:.1f}%`",
                f"- **Tier 2 `P(Interview | Applied)`:** `{result.posterior_rates['Tier 2']*100:.1f}%`",
                f"- **Tier 3 `P(Interview | Applied)`:** `{result.posterior_rates['Tier 3']*100:.1f}%`",
                f"- **`P(Offer | Interview)`:** `{result.offer_given_interview_rate*100:.1f}%`",
                f"- **Currently In-Flight Applications:** `{len(snap.in_flight_apps)}` (`Applied` <30d or `Interviewing`)",
            ]
        )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def render_forecast_cli(result: ForecastResult, console: Optional[Console] = None) -> None:
    """Print a concise Rich forecast summary in the terminal."""
    out = console or Console()
    fmt_d = lambda d: d.strftime("%b %d, %Y") if d else "Beyond Horizon"

    tbl = Table(title=f"Offer Date Forecast (Target: {result.target_date.strftime('%b %d, %Y')})")
    tbl.add_column("Metric", style="bold cyan")
    tbl.add_column("Value", style="bold white")
    tbl.add_row("Trailing Weekly Pace Used", f"{result.weekly_pace_used:.1f} apps/week")
    tbl.add_row("In-Flight Pipeline", f"{len(result.snapshot.in_flight_apps) if result.snapshot else 0} active applications")
    tbl.add_row("Optimistic Offer (P25)", fmt_d(result.p25_date))
    tbl.add_row("Median Offer (P50)", f"[green]{fmt_d(result.p50_date)}[/green]")
    tbl.add_row("Conservative Offer (P80)", fmt_d(result.p80_date))
    tbl.add_row(f"Prob. by {result.target_date.strftime('%b %d, %Y')}", f"[bold yellow]{result.prob_by_target:.1f}%[/bold yellow]")
    tbl.add_row("Recommended Pace (>=80%)", f"{result.recommended_weekly_pace:.0f} apps/week")
    out.print(tbl)
    for alert in result.alerts:
        out.print(f"  [yellow]•[/yellow] {alert}")


def main() -> None:
    console = Console()
    engine = init_db()
    SessionLocal = get_session_factory(engine)
    with SessionLocal() as session:
        if is_notion_configured():
            sync_res = sync_notion_board_transitions(session, console=console)
            console.print(
                f"[cyan]Notion Board Sync:[/cyan] Scanned {sync_res['scanned']} cards, "
                f"logged {sync_res['events_logged']} transitions, auto-stamped {sync_res['dates_stamped']} Applied dates."
            )
        result = run_offer_forecast(session)
        render_forecast_cli(result, console=console)
        report_path = write_forecast_markdown(result)
        console.print(f"[green]Forecast report saved:[/green] {report_path}")
        if is_notion_configured():
            update_notion_board_counts_banner(forecast_summary=format_banner_summary(result))


if __name__ == "__main__":
    main()
