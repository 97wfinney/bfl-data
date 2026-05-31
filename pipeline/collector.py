"""collector.py - save a full bootstrap-static snapshot for the current GW.

The file is named by gameweek and overwritten on each run, so it keeps
updating through a live GW and freezes once the next GW becomes current.
"""
from __future__ import annotations

import fpl_common as fc


def run(bootstrap=None, season=None):
    bootstrap = bootstrap or fc.fetch_bootstrap()
    if not bootstrap:
        fc.log("collector: no bootstrap; skipping.")
        return
    season = season or fc.derive_season()
    gw = fc.current_gameweek(bootstrap)
    if gw is None:
        fc.log("collector: no current gameweek; skipping.")
        return
    path = fc.bootstrap_dir(season) / f"Gameweek_{gw}.json"
    fc.write_json(path, bootstrap)
    fc.log(f"collector: saved snapshot -> {path.relative_to(fc.REPO_ROOT)}")


if __name__ == "__main__":
    run()
