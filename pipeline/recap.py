"""recap.py - the BFL weekly league report.

Curates the league's own story for the latest FINISHED gameweek from data the
pipeline already collected (league.json + each manager's enriched picks) plus a
single live-event fetch for real player points, then asks gpt-5.5 to write it up.

Two pieces of prose come back: a punchy `summary` for the homepage and a full
`report` for the dedicated weekly-report page. The deterministic `facts` block
is saved alongside so the site can render structured award cards - and so the
model only ever phrases numbers it was given, never invents them.

Writes:
  data/<season>/reports/recap.json            - latest finished GW (homepage)
  data/<season>/reports/recaps/gw<n>.json     - per-GW archive (report page)
  data/<season>/reports/recaps/index.json     - list of available recaps

A finished GW is generated once and then cached (it can't change), so this
costs at most one AI call per week. Set RECAP_FORCE=1 to regenerate.
"""
from __future__ import annotations

import glob
import json
import os
import re

import fpl_common as fc

MAX_OUTPUT_TOKENS = 2000

SYSTEM_PROMPT = (
    "You are the writer for the Biddenham Fantasy League (BFL), a Fantasy Premier "
    "League mini-league among friends and colleagues. You are given a JSON facts "
    "sheet for one finished gameweek. Write the league's weekly report using ONLY "
    "those facts - never invent a manager, player, score, gap or event; every "
    "number and name you use must come from the facts. Produce three things.\n\n"
    "headline: one punchy line capturing the gameweek's biggest story.\n\n"
    "summary: the homepage teaser - 2 to 3 sentences, punchy and specific, naming "
    "the standout manager(s) and the state of the title race. This is what most "
    "people read, so make it land.\n\n"
    "report: the full write-up - several lively paragraphs (separated by blank "
    "lines) that tell the story of the gameweek: the title race and any movement, "
    "the manager of the week and how they did it, the captaincy calls (heroes and "
    "howlers), standout differentials, bench disasters, chips played, and any "
    "tight battles down the table. Name managers and players, use the exact "
    "numbers, and write it like a newsletter everyone in the league actually wants "
    "to read - warm, a bit of banter, never dry. No preamble or sign-off.\n\n"
    "Return ONLY valid JSON - no markdown, no code fences - in exactly this shape: "
    '{"headline": "...", "summary": "...", "report": "..."}'
)


# --- gameweek resolution -------------------------------------------------
def _latest_finished_gw(bootstrap):
    events = bootstrap.get("events", []) if bootstrap else []
    finished = [e["id"] for e in events if e.get("finished") and isinstance(e.get("id"), int)]
    return max(finished) if finished else None


def _event_row(bootstrap, gw):
    for e in bootstrap.get("events", []):
        if e.get("id") == gw:
            return e
    return {}


def _total_gws(bootstrap):
    ids = [e["id"] for e in bootstrap.get("events", []) if isinstance(e.get("id"), int)]
    return max(ids) if ids else 38


# --- data access ---------------------------------------------------------
def _live_points(gw):
    """element id -> total points for the gameweek (final once finished)."""
    data = fc.fetch_json(fc.BASE_URL + f"event/{gw}/live/")
    if not data:
        return {}
    return {e["id"]: (e.get("stats") or {}).get("total_points", 0)
            for e in data.get("elements", [])}


def _target_picks(season, eid, gw):
    return fc.read_json(fc.entries_dir(season) / str(eid) / "picks" / f"gw{gw}.json")


# --- standings helpers ---------------------------------------------------
def _ranks(pairs):
    """pairs: list of (eid, value). Returns {eid: rank} ranked by value desc."""
    ordered = sorted(pairs, key=lambda x: x[1], reverse=True)
    return {eid: i + 1 for i, (eid, _) in enumerate(ordered)}


def _round1(x):
    try:
        return round(float(x), 1)
    except (TypeError, ValueError):
        return None


# --- fact curation -------------------------------------------------------
def _curate(season, bootstrap, gw, league):
    managers = league.get("managers", [])
    ev = _event_row(bootstrap, gw)
    live = _live_points(gw)

    # Per-manager gameweek + cumulative figures, pulled from history by GW.
    M = {}
    for m in managers:
        eid = m.get("entry")
        rows = {r.get("gw"): r for r in m.get("history", []) if r.get("gw") is not None}
        if gw not in rows:
            continue
        cum = sum((rows[g].get("net") or 0) for g in rows if g <= gw)
        gw_net = rows[gw].get("net") or 0
        M[eid] = {
            "entry": eid,
            "manager": m.get("manager", ""),
            "team": m.get("team", ""),
            "gw_gross": rows[gw].get("points") or 0,
            "gw_hits": rows[gw].get("hits") or 0,
            "gw_net": gw_net,
            "gw_bench": rows[gw].get("bench") or 0,
            "overall_rank": rows[gw].get("overall_rank"),
            "cum": cum,
            "prev_cum": cum - gw_net,
            "chips_used": m.get("chips_used", []) or [],
            "rows": rows,
        }
    if not M:
        return None

    n = len(M)
    cur_rank = _ranks([(e, d["cum"]) for e, d in M.items()])
    prev_rank = _ranks([(e, d["prev_cum"]) for e, d in M.items()])
    for e, d in M.items():
        d["rank"] = cur_rank[e]
        d["movement"] = prev_rank[e] - cur_rank[e]   # +ve = climbed

    ordered = sorted(M.values(), key=lambda d: d["cum"], reverse=True)
    prev_ordered = sorted(M.values(), key=lambda d: d["prev_cum"], reverse=True)

    def slim(d):
        return {"manager": d["manager"], "team": d["team"], "total": d["cum"],
                "gw_net": d["gw_net"], "rank": d["rank"], "movement": d["movement"]}

    leader, second = ordered[0], (ordered[1] if n > 1 else None)
    title_race = {
        "leader": {"manager": leader["manager"], "team": leader["team"], "total": leader["cum"]},
        "second": ({"manager": second["manager"], "team": second["team"],
                    "total": second["cum"], "gap": leader["cum"] - second["cum"]}
                   if second else None),
        "lead_changed": bool(second) and prev_ordered[0]["entry"] != leader["entry"],
        "top5": [slim(d) for d in ordered[:5]],
    }

    # Gameweek scoring
    by_net = sorted(M.values(), key=lambda d: d["gw_net"], reverse=True)
    motw, spoon = by_net[0], by_net[-1]
    league_avg = _round1(sum(d["gw_gross"] for d in M.values()) / n)
    gameweek = {
        "league_avg": league_avg,
        "global_avg": ev.get("average_entry_score"),
        "global_highest": ev.get("highest_score"),
        "manager_of_week": {"manager": motw["manager"], "team": motw["team"],
                            "gross": motw["gw_gross"], "hits": motw["gw_hits"],
                            "net": motw["gw_net"]},
        "wooden_spoon": {"manager": spoon["manager"], "team": spoon["team"],
                         "gross": spoon["gw_gross"], "net": spoon["gw_net"]},
        "top_scores": [{"manager": d["manager"], "net": d["gw_net"],
                        "gross": d["gw_gross"]} for d in by_net[:3]],
    }

    movers = {
        "climbers": [{"manager": d["manager"], "movement": d["movement"], "gw_net": d["gw_net"]}
                     for d in sorted(M.values(), key=lambda d: d["movement"], reverse=True)
                     if d["movement"] > 0][:3],
        "fallers": [{"manager": d["manager"], "movement": d["movement"], "gw_net": d["gw_net"]}
                    for d in sorted(M.values(), key=lambda d: d["movement"])
                    if d["movement"] < 0][:3],
    }

    # Ownership / captaincy / chips - from this GW's picks
    owned, started, meta = {}, {}, {}
    owners = {}
    captains = []          # (manager, element, multiplier, web_name)
    chips_played = []
    for eid, d in M.items():
        picks = _target_picks(season, eid, gw)
        if not picks:
            continue
        chip = picks.get("active_chip")
        if chip:
            chips_played.append({"manager": d["manager"], "chip": chip, "gw_net": d["gw_net"]})
        for p in picks.get("picks", []):
            el = p.get("element")
            if el is None:
                continue
            owned[el] = owned.get(el, 0) + 1
            owners.setdefault(el, []).append(d["manager"])
            meta.setdefault(el, {"web_name": p.get("web_name", ""), "team": p.get("team", "")})
            if (p.get("position") or 99) <= 11:
                started[el] = started.get(el, 0) + 1
            if p.get("is_captain"):
                captains.append((d["manager"], el, p.get("multiplier", 2), p.get("web_name", "")))

    captaincy = None
    if captains:
        cap_points = [(mgr, el, mult, name, live.get(el, 0)) for (mgr, el, mult, name) in captains]
        genius = max(cap_points, key=lambda x: x[4])
        howler = min(cap_points, key=lambda x: x[4])
        cap_counts = {}
        for (_, el, _, name, _) in cap_points:
            cap_counts.setdefault(el, {"name": name, "count": 0})
            cap_counts[el]["count"] += 1
        pop_el = max(cap_counts, key=lambda e: cap_counts[e]["count"])
        captaincy = {
            "most_popular": {"web_name": cap_counts[pop_el]["name"],
                             "count": cap_counts[pop_el]["count"],
                             "points": live.get(pop_el, 0)},
            "genius": {"manager": genius[0], "web_name": genius[3],
                       "points": genius[4], "multiplier": genius[2]},
            "howler": {"manager": howler[0], "web_name": howler[3], "points": howler[4]},
        }

    # Differentials (owned by <= 2) and template (most owned), with real points
    differentials = sorted(
        [{"web_name": meta[el]["web_name"], "team": meta[el]["team"],
          "points": live.get(el, 0), "owned": owned[el], "owners": sorted(owners[el])}
         for el in owned if owned[el] <= 2],
        key=lambda x: x["points"], reverse=True,
    )[:6]
    template = sorted(
        [{"web_name": meta[el]["web_name"], "team": meta[el]["team"],
          "owned": owned[el], "owned_pct": _round1(100 * owned[el] / n),
          "points": live.get(el, 0)}
         for el in owned],
        key=lambda x: (x["owned"], x["points"]), reverse=True,
    )[:8]

    # Bench agony, hits, form, chips remaining, season record
    bench = max(M.values(), key=lambda d: d["gw_bench"])
    bench_agony = {"manager": bench["manager"], "bench_points": bench["gw_bench"]}

    takers = [{"manager": d["manager"], "cost": d["gw_hits"], "gw_gross": d["gw_gross"],
               "gw_net": d["gw_net"]} for d in M.values() if d["gw_hits"] > 0]
    takers.sort(key=lambda x: x["cost"], reverse=True)
    hits = {"takers": takers[:5], "biggest": (takers[0] if takers else None)}

    def last3(d):
        return sum((d["rows"][g].get("net") or 0) for g in (gw - 2, gw - 1, gw) if g in d["rows"])
    hottest = max(M.values(), key=last3)
    coldest = min(M.values(), key=last3)
    form = {"hottest": {"manager": hottest["manager"], "last3_net": last3(hottest)},
            "coldest": {"manager": coldest["manager"], "last3_net": last3(coldest)}}

    chips = {
        "played": chips_played,
        "remaining": {
            "triple_captain": sum(1 for d in M.values() if "3xc" not in d["chips_used"]),
            "bench_boost": sum(1 for d in M.values() if "bboost" not in d["chips_used"]),
            "free_hit": sum(1 for d in M.values() if "freehit" not in d["chips_used"]),
        },
    }

    best = {"manager": None, "gw": None, "net": -10**9}
    for d in M.values():
        for g, r in d["rows"].items():
            net = r.get("net") or 0
            if net > best["net"]:
                best = {"manager": d["manager"], "gw": g, "net": net}

    # Tight battles: adjacent pairs within 6 points
    battles = []
    for a, b in zip(ordered, ordered[1:]):
        gap = a["cum"] - b["cum"]
        if gap <= 6:
            battles.append({"higher": a["manager"], "lower": b["manager"],
                            "gap": gap, "ranks": [a["rank"], b["rank"]]})

    return {
        "league": (league.get("league") or {}).get("name", "Biddenham Fantasy League"),
        "season": season,
        "gw": gw,
        "manager_count": n,
        "gws_remaining": _total_gws(bootstrap) - gw,
        "title_race": title_race,
        "gameweek": gameweek,
        "movers": movers,
        "captaincy": captaincy,
        "chips": chips,
        "hits": hits,
        "differentials": differentials,
        "template": template,
        "bench_agony": bench_agony,
        "form": form,
        "season_record": best,
        "battles": battles[:3],
    }


# --- AI write-up ---------------------------------------------------------
def _parse_writeup(text, facts):
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
        return {
            "headline": str(obj.get("headline", "")).strip(),
            "summary": str(obj.get("summary", "")).strip(),
            "report": str(obj.get("report", "")).strip(),
        }
    except Exception:
        return _fallback(facts)


def _fallback(facts):
    """Deterministic prose if the model is unavailable or returns junk."""
    tr = facts["title_race"]
    motw = facts["gameweek"]["manager_of_week"]
    lead = tr["leader"]
    summary = f"{lead['manager']} leads the BFL on {lead['total']} points"
    if tr.get("second"):
        summary += f", {tr['second']['gap']} clear of {tr['second']['manager']}"
    summary += f". {motw['manager']} topped GW{facts['gw']} with {motw['net']}."
    return {"headline": f"GW{facts['gw']}: {lead['manager']} stays top",
            "summary": summary, "report": summary}


def _writeup(facts):
    if not os.getenv("OPENAI_API_KEY"):
        fc.log("recap: OPENAI_API_KEY missing; using deterministic fallback.")
        return _fallback(facts)
    try:
        from openai import OpenAI
        client = OpenAI()
        resp = client.responses.create(
            model="gpt-5.5",
            instructions=SYSTEM_PROMPT,
            input=json.dumps(facts, ensure_ascii=False),
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        return _parse_writeup(resp.output_text, facts)
    except Exception as e:
        fc.log(f"recap: AI write-up failed ({e}); using fallback.")
        return _fallback(facts)


# --- index ---------------------------------------------------------------
def _rebuild_index(season):
    recaps_dir = fc.reports_dir(season) / "recaps"
    items = []
    for fp in glob.glob(str(recaps_dir / "gw*.json")):
        d = fc.read_json(fp) or {}
        if d.get("gw") is not None:
            items.append({"gw": d["gw"], "headline": d.get("headline", ""),
                          "generated_at": d.get("generated_at")})
    items.sort(key=lambda x: x["gw"], reverse=True)
    fc.write_json(recaps_dir / "index.json", {"updated_at": fc.now_iso(), "recaps": items})


# --- entry point ---------------------------------------------------------
def run(bootstrap=None, season=None):
    bootstrap = bootstrap or fc.fetch_bootstrap()
    if not bootstrap:
        fc.log("recap: no bootstrap; skipping.")
        return
    season = season or fc.derive_season()

    gw = _latest_finished_gw(bootstrap)
    if not gw:
        fc.log("recap: no finished gameweek yet; skipping.")
        return

    recaps_dir = fc.reports_dir(season) / "recaps"
    archive = recaps_dir / f"gw{gw}.json"
    latest = fc.reports_dir(season) / "recap.json"
    force = os.getenv("RECAP_FORCE") == "1"

    if archive.exists() and not force:
        cached = fc.read_json(archive)
        if cached:
            fc.write_json(latest, cached)        # keep homepage pointer fresh
            _rebuild_index(season)
            fc.log(f"recap: GW{gw} already generated; pointer refreshed.")
            return

    league = fc.read_json(fc.reports_dir(season) / "league.json")
    if not league:
        fc.log("recap: league.json missing; run report first.")
        return

    facts = _curate(season, bootstrap, gw, league)
    if not facts:
        fc.log("recap: could not curate facts; skipping.")
        return

    prose = _writeup(facts)
    payload = {
        "season": season,
        "gw": gw,
        "league": facts["league"],
        "generated_at": fc.now_iso(),
        "manager_count": facts["manager_count"],
        "gws_remaining": facts["gws_remaining"],
        "headline": prose["headline"],
        "summary": prose["summary"],
        "report": prose["report"],
        "facts": facts,
    }

    fc.write_json(archive, payload)
    fc.write_json(latest, payload)
    _rebuild_index(season)
    fc.log(f"recap: GW{gw} report written ({facts['manager_count']} managers).")


if __name__ == "__main__":
    run()
