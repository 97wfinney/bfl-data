"""run_pipeline.py - nightly orchestrator for the BFL data pipeline.

Fetches the bootstrap once, derives the season + gameweek, ensures the
season's folders exist, runs every producer in order off that shared
bootstrap, then makes a single git commit/push. Each step is isolated so one
failure can't take down the rest - whatever succeeded still ships.

Point cron at this file:
  0 1 * * * cd ~/Desktop/bfl-data && .venv/bin/python pipeline/run_pipeline.py >> ~/bfl-pipeline.log 2>&1
"""
from __future__ import annotations

import traceback

import fpl_common as fc
import collector
import league
import entries
import report
import summaries

STEPS = [
    ("collector", collector.run),
    ("league", league.run),
    ("entries", entries.run),
    ("report", report.run),
    ("summaries", summaries.run),
]


def main():
    fc.log(f"=== pipeline start {fc.now_iso()} ===")

    bootstrap = fc.fetch_bootstrap()
    if not bootstrap:
        fc.log("pipeline: bootstrap fetch failed - aborting.")
        return

    season = fc.derive_season()
    gw = fc.current_gameweek(bootstrap)
    fc.ensure_season_dirs(season)
    fc.log(f"pipeline: season {season}, GW {gw}")

    for name, fn in STEPS:
        try:
            fn(bootstrap=bootstrap, season=season)
        except Exception:
            fc.log(f"pipeline: STEP '{name}' FAILED:\n{traceback.format_exc()}")

    ev = fc.current_event(bootstrap)
    state = "final" if (ev and ev.get("finished")) else "in progress"
    fc.commit_and_push(f"Update {season} data - GW{gw} ({state})")
    fc.log(f"=== pipeline done {fc.now_iso()} ===")


if __name__ == "__main__":
    main()
