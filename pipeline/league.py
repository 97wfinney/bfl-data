"""league.py - save classic mini-league standings per gameweek.

Finished gameweeks are immutable (fetched once and cached). The current,
in-progress gameweek is re-fetched and overwritten every run, so a 1am run
mid-GW is correct as of that moment and finalises once the GW completes.
Any missing past gameweeks are backfilled automatically (no manual sync).
"""
from __future__ import annotations

import fpl_common as fc


def run(bootstrap=None, season=None):
    bootstrap = bootstrap or fc.fetch_bootstrap()
    if not bootstrap:
        fc.log("league: no bootstrap; skipping.")
        return
    season = season or fc.derive_season()

    cur = fc.current_gameweek(bootstrap)
    out_dir = fc.standings_dir(season)
    saved = 0

    for gw in fc.finished_or_current_gws(bootstrap):
        path = out_dir / f"mini_league_gw{gw}.json"
        is_live = (gw == cur) and not fc.is_finished(bootstrap, gw)
        if path.exists() and not is_live:
            continue                      # finished GW already saved
        data = fc.fetch_json(
            fc.BASE_URL + f"leagues-classic/{fc.LEAGUE_ID}/standings/?event={gw}"
        )
        if data:
            fc.write_json(path, data)
            saved += 1
    fc.log(f"league: standings written/updated for {saved} gameweek(s).")


if __name__ == "__main__":
    run()
