"""insights.py - league-ownership intelligence for the current gameweek.

Reads every manager's picks for the current GW (collected by entries.py),
aggregates them, and writes a single small file the dashboard can read in one
fetch:

  data/<season>/reports/insights.json

Per player it records how many league managers own them, how many start them,
how many captain them, and (for differentials owned by <= 2 managers) exactly
who. The site slices this one array into ownership / captaincy / differentials /
template views.
"""
from __future__ import annotations

import glob
from collections import defaultdict

import fpl_common as fc


def _player_lookup(bootstrap):
    elements = {e["id"]: e for e in bootstrap.get("elements", [])}
    teams = {t["id"]: t.get("short_name", "") for t in bootstrap.get("teams", [])}
    types = {t["id"]: t.get("singular_name_short", "") for t in bootstrap.get("element_types", [])}
    out = {}
    for pid, e in elements.items():
        out[pid] = {
            "web_name": e.get("web_name", ""),
            "team": teams.get(e.get("team"), ""),
            "pos": types.get(e.get("element_type"), ""),
        }
    return out


def _entry_names(season):
    idx = fc.read_json(fc.entries_dir(season) / "entries_index.json") or {}
    return {e["entry"]: (e.get("player_name") or e.get("entry_name") or str(e["entry"]))
            for e in idx.get("entries", [])}


def _read_picks(season, eid, gw):
    """Picks for the target GW, or the manager's latest picks file if missing."""
    path = fc.entries_dir(season) / str(eid) / "picks" / f"gw{gw}.json"
    data = fc.read_json(path)
    if data:
        return data
    files = sorted(
        glob.glob(str(fc.entries_dir(season) / str(eid) / "picks" / "gw*.json")),
        key=lambda p: int(p.split("gw")[-1].split(".")[0]),
    )
    return fc.read_json(files[-1]) if files else None


def run(bootstrap=None, season=None):
    bootstrap = bootstrap or fc.fetch_bootstrap()
    if not bootstrap:
        fc.log("insights: no bootstrap; skipping.")
        return
    season = season or fc.derive_season()
    gw = fc.current_gameweek(bootstrap)

    names = _entry_names(season)
    if not names:
        fc.log("insights: no entries index yet; run entries first.")
        return

    owned = defaultdict(int)
    started = defaultdict(int)
    captained = defaultdict(int)
    benched = defaultdict(int)
    owners = defaultdict(list)
    counted = 0

    for eid in names:
        picks = _read_picks(season, eid, gw)
        if not picks:
            continue
        counted += 1
        for p in picks.get("picks", []):
            el = p.get("element")
            if el is None:
                continue
            owned[el] += 1
            owners[el].append(names.get(eid, str(eid)))
            if (p.get("position") or 99) <= 11:
                started[el] += 1
            else:
                benched[el] += 1
            if p.get("is_captain"):
                captained[el] += 1

    if not counted:
        fc.log("insights: no picks found; skipping.")
        return

    pmap = _player_lookup(bootstrap)
    players = []
    for el, cnt in owned.items():
        meta = pmap.get(el, {})
        players.append({
            "element": el,
            "web_name": meta.get("web_name", ""),
            "team": meta.get("team", ""),
            "pos": meta.get("pos", ""),
            "owned": cnt,
            "owned_pct": round(100 * cnt / counted, 1),
            "started": started.get(el, 0),
            "captained": captained.get(el, 0),
            "captained_pct": round(100 * captained.get(el, 0) / counted, 1),
            "benched": benched.get(el, 0),
            "owners": sorted(owners[el]) if cnt <= 2 else None,
        })

    players.sort(key=lambda x: (x["owned"], x["captained"]), reverse=True)

    fc.write_json(fc.reports_dir(season) / "insights.json", {
        "season": season,
        "gw": gw,
        "generated_at": fc.now_iso(),
        "manager_count": counted,
        "players": players,
    })
    fc.log(f"insights: {len(players)} players across {counted} managers (GW{gw}).")


if __name__ == "__main__":
    run()
