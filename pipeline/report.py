"""report.py - build the dashboard payload and the root status file.

Reads the latest standings (for official rank + names) and each manager's
history (for the gross-points / hit-cost breakdown), computes true totals
per the BFL methodology (sum of points minus sum of hit cost), and writes:

  data/<season>/reports/league.json   - the dashboard payload
  data/status.json                    - season + current GW + finished flag
                                         (the single entry point the site reads)
"""
from __future__ import annotations

import glob

import fpl_common as fc


def _latest_standings(season):
    files = sorted(
        glob.glob(str(fc.standings_dir(season) / "mini_league_gw*.json")),
        key=lambda p: int(p.split("gw")[-1].split(".")[0]),
    )
    return fc.read_json(files[-1]) if files else None


def _breakdown(history):
    """From an entry history.json, return totals + per-GW rows."""
    cur = (history or {}).get("current", []) or []
    rows = []
    gross = hits = 0
    for r in cur:
        pts = r.get("points", 0)
        cost = r.get("event_transfers_cost", 0)
        gross += pts
        hits += cost
        rows.append({
            "gw": r.get("event"),
            "points": pts,
            "hits": cost,
            "net": pts - cost,
            "total": r.get("total_points"),
            "bench": r.get("points_on_bench", 0),
            "overall_rank": r.get("overall_rank"),
        })
    last = cur[-1] if cur else {}
    chips = [c.get("name") for c in (history or {}).get("chips", [])]
    return {
        "gross_points": gross,
        "hits": hits,
        "total": gross - hits,             # true total (BFL methodology)
        "gw_points": last.get("points", 0),
        "gw_hits": last.get("event_transfers_cost", 0),
        "chips_used": chips,
        "history": rows,
    }


def run(bootstrap=None, season=None):
    bootstrap = bootstrap or fc.fetch_bootstrap()
    season = season or fc.derive_season()

    ev = fc.current_event(bootstrap)
    cur_gw = ev.get("id") if ev else fc.current_gameweek(bootstrap)
    finished = bool(ev.get("finished")) if ev else False

    standings = _latest_standings(season)
    league_name = ((standings or {}).get("league") or {}).get("name", "")
    results = ((standings or {}).get("standings") or {}).get("results", [])

    managers = []
    for r in results:
        eid = r.get("entry")
        hist = fc.read_json(fc.entries_dir(season) / str(eid) / "history.json")
        # chip played *this* gameweek only (None if no chip this GW)
        chips = (hist or {}).get("chips", [])
        gw_chip = next((c.get("name") for c in chips if c.get("event") == cur_gw), None)
        managers.append({
            "entry": eid,
            "manager": r.get("player_name", ""),
            "team": r.get("entry_name", ""),
            "rank": r.get("rank"),
            "last_rank": r.get("last_rank"),
            "rank_movement": (r.get("last_rank") or 0) - (r.get("rank") or 0),
            "official_total": r.get("total"),
            "gw_chip": gw_chip,
            **_breakdown(hist),
        })

    managers.sort(
        key=lambda m: (m.get("official_total") or m.get("total") or 0),
        reverse=True,
    )

    fc.write_json(fc.reports_dir(season) / "league.json", {
        "season": season,
        "current_gw": cur_gw,
        "finished": finished,
        "updated_at": fc.now_iso(),
        "league": {"id": fc.LEAGUE_ID, "name": league_name},
        "managers": managers,
    })

    fc.write_json(fc.status_path(), {
        "season": season,
        "current_gw": cur_gw,
        "finished": finished,
        "updated_at": fc.now_iso(),
    })

    state = "final" if finished else "in progress"
    fc.log(f"report: league.json + status.json written (GW{cur_gw}, {state}).")


if __name__ == "__main__":
    run()
