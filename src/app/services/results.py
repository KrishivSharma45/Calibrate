"""Results: raw averages, per-judge z-score normalization, ranking, publishing.

Method (JUDGING.md explains the why):
  t        = weighted total of one score = sum(value * weight)            (1..5)
  mu_j,sd_j = mean and population std-dev of judge j's totals in this event
  z        = (t - mu_j) / sd_j                  "how much better than this judge's usual"
  w_j      = 0              if sd_j == 0   (flat judge, or a single score: no ranking signal,
                                            so their reviews abstain instead of voting 'average')
           = min(1, n_j/3)  otherwise      (judges with fewer than 3 scores count less)
  zbar_p   = sum(w_j * z_pj) / sum(w_j)   over project p's reviews     (None if sum(w) == 0)
  normalized_p = mu + zbar_p * sd          (mu, sd over all totals in the event: back on the 1..5 scale)
Ranking: normalized desc, then raw mean desc, then review count desc, then project id.
Only submitted, non-duplicate projects count; scores on a flagged duplicate are ignored,
so the fixture's prj_07/prj_41 pair is not double counted.
"""

from collections import defaultdict
from statistics import fmean, pstdev

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from app.audit import audit
from app.models import Event, Project, ProjectStatus, Score, User
from app.services.judging import criteria_for, weighted_total
from app.services.projects import db_now

FULL_CONFIDENCE_AT = 3      # a judge needs this many scores for full weight
LOW_CONFIDENCE_BELOW = 2.0  # a project whose reviewer weights sum below this is flagged


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def compute_results(db: DbSession, event: Event) -> dict:
    projects = {p.id: p for p in db.scalars(
        select(Project).where(Project.event_id == event.id, Project.status == ProjectStatus.submitted,
                              Project.duplicate_of_id.is_(None))
        .options(selectinload(Project.track)))}
    criteria = criteria_for(db, event.id)
    scores = db.scalars(select(Score).where(Score.project_id.in_(projects))
                        .options(selectinload(Score.items))).all()

    totals: list[tuple[str, str, float]] = []   # (judge, project, weighted total)
    for s in scores:
        t = weighted_total(s, criteria)
        if t is not None:
            totals.append((s.judge_id, s.project_id, t))

    # --- per judge ---
    by_judge: dict[str, list[float]] = defaultdict(list)
    for j, _, t in totals:
        by_judge[j].append(t)
    names = {u.id: u.name for u in db.scalars(select(User).where(User.id.in_(by_judge)))}
    judge_stats = {}
    for j, ts in by_judge.items():
        n, mu, sd = len(ts), fmean(ts), pstdev(ts)
        if sd == 0:
            weight, flag = 0.0, "single_score" if n == 1 else "flat"
        else:
            weight = min(1.0, n / FULL_CONFIDENCE_AT)
            flag = "few_scores" if n < FULL_CONFIDENCE_AT else None
        judge_stats[j] = {"judge_id": j, "name": names.get(j, j), "n": n, "mean": mu, "sd": sd,
                          "weight": weight, "flag": flag}

    # --- per project ---
    event_mu = fmean([t for _, _, t in totals]) if totals else None
    event_sd = pstdev([t for _, _, t in totals]) if len(totals) > 1 else 0.0
    reviews: dict[str, list[tuple[str, float, float]]] = defaultdict(list)   # project -> (judge, t, z)
    for j, p, t in totals:
        st = judge_stats[j]
        z = (t - st["mean"]) / st["sd"] if st["sd"] > 0 else 0.0
        reviews[p].append((j, t, z))

    rows = []
    for pid, project in projects.items():
        rs = reviews.get(pid, [])
        raw_mean = fmean([t for _, t, _ in rs]) if rs else None
        weight_sum = sum(judge_stats[j]["weight"] for j, _, _ in rs)
        zbar = (sum(judge_stats[j]["weight"] * z for j, _, z in rs) / weight_sum) if weight_sum > 0 else None
        normalized = event_mu + zbar * event_sd if zbar is not None else None
        flags = []
        if not rs:
            flags.append("no_reviews")
        elif zbar is None:
            flags.append("no_informative_reviews")
        elif weight_sum < LOW_CONFIDENCE_BELOW:
            flags.append("low_confidence")
        flagged_judges = sorted({j for j, _, _ in rs if judge_stats[j]["flag"]})
        rows.append({
            "project_id": pid, "title": project.title, "track_id": project.track_id,
            "track": project.track.name if project.track else None, "reviews": len(rs),
            "effective_reviews": _r(weight_sum), "raw_mean": _r(raw_mean), "z_mean": _r(zbar),
            "normalized": _r(normalized), "flags": flags, "flagged_judges": flagged_judges,
            "rank": None, "track_rank": None, "raw_rank": None,
        })

    def rank(items, key_rank: str, key_fn, filt=lambda r: True):
        ranked = sorted((r for r in items if filt(r)), key=key_fn)
        for i, r in enumerate(ranked, start=1):
            r[key_rank] = i

    order = lambda r: (-r["normalized"], -(r["raw_mean"] or 0), -r["reviews"], r["project_id"])  # noqa: E731
    rank(rows, "rank", order, lambda r: r["normalized"] is not None)
    for track_id in {r["track_id"] for r in rows}:
        rank(rows, "track_rank", order, lambda r, t=track_id: r["normalized"] is not None and r["track_id"] == t)
    rank(rows, "raw_rank", lambda r: (-r["raw_mean"], -r["reviews"], r["project_id"]),
         lambda r: r["raw_mean"] is not None)
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["title"].lower()))

    judges = sorted(({**s, "mean": _r(s["mean"]), "sd": _r(s["sd"]), "weight": _r(s["weight"])}
                     for s in judge_stats.values()), key=lambda s: s["judge_id"])
    return {
        "event_id": event.id, "published_at": event.results_published_at.isoformat()
        if event.results_published_at else None,
        "method": "per-judge z-score, confidence-weighted mean, rescaled to event mean/sd",
        "event_mean": _r(event_mu), "event_sd": _r(event_sd),
        "projects": rows, "judges": judges,
    }


def public_results(results: dict) -> dict:
    """What non-staff see after publishing: ranks and scores, no per-judge data or flags on judges."""
    keep = ("rank", "track_rank", "project_id", "title", "track", "reviews", "normalized", "raw_mean")
    return {"event_id": results["event_id"], "published_at": results["published_at"],
            "method": results["method"],
            "projects": [{k: r[k] for k in keep} for r in results["projects"]]}


# --- publishing ---------------------------------------------------------------------------------


def publish(db: DbSession, actor: User, event: Event) -> Event:
    now = db_now(db)
    if now < event.submissions_close:
        raise HTTPException(status_code=409, detail={
            "error": "submissions_still_open", "message": "Close submissions before publishing results."})
    if event.results_published_at is None:
        event.results_published_at = now
        audit(db, actor, "results.publish", "event", event.id)
        db.commit()
    return event


def unpublish(db: DbSession, actor: User, event: Event) -> Event:
    if event.results_published_at is not None:
        audit(db, actor, "results.unpublish", "event", event.id, was_published_at=event.results_published_at)
        event.results_published_at = None
        db.commit()
    return event
