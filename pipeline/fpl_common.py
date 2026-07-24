"""
fpl_common.py - shared helpers for the BFL data pipeline.

One place for: config, the FPL API session (with retry), the bootstrap fetch,
season + gameweek derivation, path helpers rooted at <repo>/data, JSON I/O,
and the single git commit/push used by the orchestrator.

Paths are derived from this file's location, so nothing is hardcoded to a
particular machine - clone the repo anywhere and it still works.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# --- Paths ---
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO_ROOT / "data"

load_dotenv(REPO_ROOT / ".env")

# --- Config ---
LEAGUE_ID = 35621
BASE_URL = "https://fantasy.premierleague.com/api/"
HEADERS = {"User-Agent": "bfl-data-pipeline (+https://fantasy.premierleague.com)"}

# --- HTTP ---
_session = requests.Session()
_session.headers.update(HEADERS)


def log(msg: str):
    print(msg, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_json(url: str, max_retries: int = 4, backoff: float = 0.8):
    """GET JSON with retry/backoff. Returns None on 403/404 or after retries."""
    attempt = 0
    while True:
        try:
            resp = _session.get(url, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (403, 404):
                log(f"  warn {resp.status_code} for {url}")
                return None
            log(f"  err {resp.status_code} for {url}")
        except requests.RequestException as e:
            log(f"  err request {url}: {e}")
        attempt += 1
        if attempt > max_retries:
            return None
        time.sleep(backoff * (2 ** (attempt - 1)))


def fetch_bootstrap():
    return fetch_json(BASE_URL + "bootstrap-static/")


# --- Season + gameweek derivation ---
def derive_season(today: "datetime | None" = None, bootstrap=None) -> str:
    """Return a season code like '2627'.

    Preference order:
      1. SEASON env var override
      2. GW1's deadline year from the bootstrap (authoritative)
      3. Date fallback - seasons can now go live as early as July
    """
    override = os.getenv("SEASON")
    if override:
        return override.strip()

    if bootstrap:
        events = bootstrap.get("events") or []
        gw1 = next((e for e in events if e.get("id") == 1), None)
        deadline = (gw1 or {}).get("deadline_time")
        if deadline:
            try:
                start = datetime.fromisoformat(deadline.replace("Z", "+00:00")).year
                return f"{start % 100:02d}{(start + 1) % 100:02d}"
            except ValueError:
                pass

    today = today or datetime.now()
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"

def current_gameweek(bootstrap) -> "int | None":
    events = bootstrap.get("events", []) if bootstrap else []
    for e in events:
        if e.get("is_current"):
            return e.get("id")
    finished = [e["id"] for e in events if e.get("finished")]
    if finished:
        return max(finished)
    for e in events:                       # pre-season fallback
        if e.get("is_next"):
            return e.get("id")
    return None


def current_event(bootstrap):
    """Return the current event dict (or the latest finished one) for status flags."""
    events = bootstrap.get("events", []) if bootstrap else []
    for e in events:
        if e.get("is_current"):
            return e
    finished = [e for e in events if e.get("finished")]
    return max(finished, key=lambda e: e["id"]) if finished else None


def finished_or_current_gws(bootstrap) -> list:
    events = bootstrap.get("events", []) if bootstrap else []
    gws = [e["id"] for e in events if e.get("finished") or e.get("is_current")]
    return sorted(set(g for g in gws if isinstance(g, int)))


def is_finished(bootstrap, gw) -> bool:
    for e in bootstrap.get("events", []):
        if e.get("id") == gw:
            return bool(e.get("finished"))
    return False


# --- Path helpers ---
def season_dir(season: str) -> Path:
    return DATA_ROOT / season


def bootstrap_dir(season): return season_dir(season) / "bootstrap"
def standings_dir(season): return season_dir(season) / "mini_league" / "standings"
def entries_dir(season): return season_dir(season) / "mini_league" / "entries"
def reports_dir(season): return season_dir(season) / "reports"
def summaries_dir(season): return season_dir(season) / "summaries"
def state_dir(season): return season_dir(season) / "state"
def status_path() -> Path: return DATA_ROOT / "status.json"


def ensure_season_dirs(season: str):
    for p in (bootstrap_dir(season), standings_dir(season), entries_dir(season),
              reports_dir(season), summaries_dir(season), state_dir(season)):
        p.mkdir(parents=True, exist_ok=True)


# --- JSON I/O ---
def write_json(path, data, indent: int = 2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)


def read_json(path):
    path = Path(path)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --- Git (orchestrator only) ---
def commit_and_push(message: str):
    """Stage everything under data/, commit if there are changes, push to main."""
    subprocess.run(["git", "-C", str(REPO_ROOT), "add", "data"], check=False)
    staged = subprocess.run(["git", "-C", str(REPO_ROOT), "diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        log("git: no changes to commit.")
        return
    subprocess.run(["git", "-C", str(REPO_ROOT), "commit", "-m", message], check=False)
    push = subprocess.run(["git", "-C", str(REPO_ROOT), "push", "origin", "main"])
    log("git: pushed." if push.returncode == 0 else "git: push FAILED.")
