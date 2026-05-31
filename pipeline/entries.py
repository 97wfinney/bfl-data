"""entries.py - collect rich per-entry data for every mini-league manager.

Caching rule:
  - entry / history / transfers: refreshed every run (they change each GW).
  - picks per GW: finished GWs are cached (skipped if present); the current
    GW's picks are always re-fetched so mid-GW data updates and then finalises.

Picks are enriched in place with player names/teams/positions from the
bootstrap, so the report layer never has to re-map element ids.
"""
from __future__ import annotations

import glob
import time

import fpl_common as fc


def _player_lookup(bootstrap):
    elements = {e["id"]: e for e in bootstrap.get("elements", [])}
    teams = {t["id"]: t.get("short_name", "") for t in bootstrap.get("teams", [])}
    types = {t["id"]: t.get("singular_name_short", "")
             for t in bootstrap.get("element_types", [])}
    out = {}
    for pid, e in elements.items():
        out[pid] = {
            "name": f"{e.get('first_name', '')} {e.get('second_name', '')}".strip(),
            "web_name": e.get("web_name", ""),
            "team": teams.get(e.get("team"), ""),
            "pos": types.get(e.get("element_type"), ""),
        }
    return out


def _discover_entries(season):
    """Aggregate entry ids + names across all saved standings files."""
    entries = {}
    pattern = str(fc.standings_dir(season) / "mini_league_gw*.json")
    for fp in sorted(glob.glob(pattern)):
        data = fc.read_json(fp) or {}
        for r in (data.get("standings") or {}).get("results", []):
            eid = r.get("entry")
            if isinstance(eid, int):
                entries[eid] = {
                    "entry": eid,
                    "entry_name": r.get("entry_name", ""),
                    "player_name": r.get("player_name", ""),
                    "rank": r.get("rank"),
                    "last_rank": r.get("last_rank"),
                }
    return entries


def _enrich_picks(picks_data, player_map):
    for p in picks_data.get("picks", []):
        info = player_map.get(p.get("element"))
        if info:
            p.update(info)
    return picks_data


def run(bootstrap=None, season=None):
    bootstrap = bootstrap or fc.fetch_bootstrap()
    if not bootstrap:
        fc.log("entries: no bootstrap; skipping.")
        return
    season = season or fc.derive_season()

    entries = _discover_entries(season)
    if not entries:
        fc.log("entries: no standings files yet; run league first.")
        return

    fc.write_json(
        fc.entries_dir(season) / "entries_index.json",
        {"generated_at": fc.now_iso(), "count": len(entries),
         "entries": list(entries.values())},
    )

    player_map = _player_lookup(bootstrap)
    cur = fc.current_gameweek(bootstrap)
    gws = fc.finished_or_current_gws(bootstrap)
    calls = 0

    for i, eid in enumerate(entries, 1):
        edir = fc.entries_dir(season) / str(eid)
        picks_dir = edir / "picks"
        picks_dir.mkdir(parents=True, exist_ok=True)

        # Always-fresh per-entry endpoints
        for name, ep in (("entry", f"entry/{eid}/"),
                         ("history", f"entry/{eid}/history/"),
                         ("transfers", f"entry/{eid}/transfers/")):
            data = fc.fetch_json(fc.BASE_URL + ep)
            calls += 1
            if data is not None:
                fc.write_json(edir / f"{name}.json", data)

        # Picks per GW: cache finished, refresh the live one
        for gw in gws:
            path = picks_dir / f"gw{gw}.json"
            is_live = (gw == cur) and not fc.is_finished(bootstrap, gw)
            if path.exists() and not is_live:
                continue
            data = fc.fetch_json(fc.BASE_URL + f"entry/{eid}/event/{gw}/picks/")
            calls += 1
            if data is not None:
                fc.write_json(path, _enrich_picks(data, player_map))
            time.sleep(0.12)               # be gentle on the API

        if i % 5 == 0:
            fc.log(f"entries: processed {i}/{len(entries)}")

    fc.log(f"entries: done. {len(entries)} entries, {calls} API calls.")


if __name__ == "__main__":
    run()
