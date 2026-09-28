"""Print raw vs normalized rankings for the fixture event, as Markdown.

Loads fixtures.json into a throw-away in-memory SQLite database with the same
seed code the portal uses, then runs the same compute_results() the portal
serves. No Docker, no network:

    python scripts/normalization_report.py > normalization-report.md
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: E402,F401
from app.config import FIXTURES_PATH  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import Event  # noqa: E402
from app.seed import run_seed  # noqa: E402
from app.services.results import compute_results  # noqa: E402


def main() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    run_seed(db, FIXTURES_PATH, demo_sessions=False)
    res = compute_results(db, db.get(Event, "evt_01"))

    print(f"Event mean of weighted totals: {res['event_mean']}, std-dev: {res['event_sd']}\n")
    print("| Norm. rank | Raw rank | Move | Project | Track | Reviews (eff.) | Raw mean | Normalized | Flags |")
    print("|---:|---:|---:|---|---|---:|---:|---:|---|")
    for r in res["projects"]:
        move = ""
        if r["rank"] and r["raw_rank"]:
            d = r["raw_rank"] - r["rank"]
            move = f"+{d}" if d > 0 else (str(d) if d < 0 else "0")
        flags = ", ".join(r["flags"] + ([f"judged by {', '.join(r['flagged_judges'])}"] if r["flagged_judges"] else []))
        norm = f"{r['normalized']:.3f}" if r["normalized"] is not None else "not ranked"
        print(f"| {r['rank'] or '–'} | {r['raw_rank'] or '–'} | {move} | {r['title']} ({r['project_id']}) | "
              f"{r['track']} | {r['reviews']} ({r['effective_reviews']}) | {r['raw_mean']:.3f} | {norm} | {flags} |")

    moved = [r for r in res["projects"] if r["rank"] and r["raw_rank"] and r["rank"] != r["raw_rank"]]
    big = sorted(moved, key=lambda r: -abs(r["raw_rank"] - r["rank"]))[:5]
    print(f"\n{len(moved)} of {sum(1 for r in res['projects'] if r['rank'])} ranked projects changed position.")
    print("Largest moves: " + "; ".join(f"{r['title']} {r['raw_rank']} -> {r['rank']}" for r in big))

    print("\n| Judge | Scores | Mean | Std-dev | Weight | Flag |")
    print("|---|---:|---:|---:|---:|---|")
    for j in res["judges"]:
        print(f"| {j['name']} ({j['judge_id']}) | {j['n']} | {j['mean']:.3f} | {j['sd']:.3f} | {j['weight']} | "
              f"{j['flag'] or ''} |")


if __name__ == "__main__":
    main()
