"""summaries.py - summarise the latest FPL YouTube uploads into a news feed.

For each channel it checks the latest few uploads, summarises any not seen
before with gpt-5.5 (Responses API), and maintains a rolling, newest-first
feed capped at FEED_LIMIT items at:

  data/<season>/summaries/feed.json

Each item now carries a structured briefing - a one-line headline, a written
multi-paragraph summary, and a list of key points - so the site can show the
headline collapsed and the full article on expand.

Dedup is tracked in data/<season>/state/processed_video_ids.txt. No Discord.
"""
from __future__ import annotations

import json
import os
import re

import fpl_common as fc

CHANNELS = {
    "Let's Talk FPL": "UCxeOc7eFxq37yW_Nc-69deA",
    "FPL Focal": "UC72QokPHXQ9r98ROfNZmaDw",
    "Fantasy Football Scout": "UCKxYKQ8pgJ7V8wwh4hLsSXQ",
    "FPL Mate": "UCweDAlFm2LnVcOqaFU4_AGA",
    "FPL Harry": "UCcPWnCj5AKC19HaySZjb25g",
}

# Higher = more videos summarised per run, but more transcript requests (and so
# more YouTube IP-block risk). Drop to 1 if blocks return.
MAX_PER_CHANNEL = 2
FEED_LIMIT = 15
MAX_OUTPUT_TOKENS = 2000

SYSTEM_PROMPT = (
    "You are an editor turning a Fantasy Premier League video into a written "
    "briefing for a terminal news feed. The written article is the main event - "
    "most readers will read your summary instead of watching - so it must stand "
    "completely on its own. From the transcript, produce three things.\n\n"
    "headline: one punchy, specific line that names the key player or angle - an "
    "editor's headline, not the video's clickbait title.\n\n"
    "summary: a self-contained article of 3-5 paragraphs in clean, flowing prose "
    "that captures the creator's actual analysis, reasoning and recommendations - "
    "the squad, captaincy and transfer thinking, WHY each call is made, and the "
    "trade-offs and risks weighed. Name players, teams and gameweeks specifically. "
    "It should read as a complete piece a reader needs nothing else to follow. "
    "Separate paragraphs with a blank line.\n\n"
    "key_points: 4-8 concrete, scannable takeaways (captaincy picks, transfers "
    "in/out, differentials, chip timing, fixture swings, price changes, "
    "injury/rotation notes).\n\n"
    "Write in present tense. Do not refer to 'the video', 'the creator' or 'this "
    "channel'; no preamble or sign-off - just the briefing. Return ONLY valid JSON "
    "- no markdown, no code fences - in exactly this shape: "
    '{"headline": "...", "summary": "...", "key_points": ["...", "..."]}'
)


def _processed_path(season):
    return fc.state_dir(season) / "processed_video_ids.txt"


def _load_processed(season):
    p = _processed_path(season)
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text().splitlines() if ln.strip()}


def _mark_processed(season, video_id):
    p = _processed_path(season)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(video_id + "\n")


def _transcript_text(video_id):
    """Works with youtube-transcript-api >=1.0 (.fetch) and <1.0 (.get_transcript)."""
    from youtube_transcript_api import YouTubeTranscriptApi
    try:
        fetched = YouTubeTranscriptApi().fetch(video_id)        # >= 1.0
        return " ".join(seg.text for seg in fetched)
    except AttributeError:
        data = YouTubeTranscriptApi.get_transcript(video_id)    # < 1.0
        return " ".join(seg["text"] for seg in data)


def _parse_briefing(text):
    """Parse the model's JSON briefing; fall back to raw text as the summary."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
        return {
            "headline": str(obj.get("headline", "")).strip(),
            "summary": str(obj.get("summary", "")).strip(),
            "key_points": [str(k).strip() for k in obj.get("key_points", []) if str(k).strip()],
        }
    except Exception:
        return {"headline": "", "summary": (text or "").strip(), "key_points": []}


def _summarise(client, transcript):
    resp = client.responses.create(
        model="gpt-5.5",
        instructions=SYSTEM_PROMPT,
        input=transcript,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    return _parse_briefing(resp.output_text)


def run(bootstrap=None, season=None):
    season = season or fc.derive_season()
    youtube_key = os.getenv("YOUTUBE_API_KEY")
    if not youtube_key:
        fc.log("summaries: YOUTUBE_API_KEY missing; skipping.")
        return
    if not os.getenv("OPENAI_API_KEY"):
        fc.log("summaries: OPENAI_API_KEY missing; skipping.")
        return

    from googleapiclient.discovery import build
    from openai import OpenAI

    processed = _load_processed(season)
    feed_path = fc.summaries_dir(season) / "feed.json"
    feed = (fc.read_json(feed_path) or {}).get("items", [])

    youtube = build("youtube", "v3", developerKey=youtube_key)
    client = OpenAI()
    new_items = 0

    for channel, cid in CHANNELS.items():
        try:
            ch = youtube.channels().list(part="contentDetails", id=cid).execute()
            if not ch.get("items"):
                fc.log(f"summaries: channel not found ({channel}); skipping.")
                continue
            uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            playlist = youtube.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=uploads,
                maxResults=MAX_PER_CHANNEL,
            ).execute()

            for item in playlist.get("items", []):
                vid = item["contentDetails"]["videoId"]
                if vid in processed:
                    continue
                title = item["snippet"]["title"]
                published = (item["contentDetails"].get("videoPublishedAt")
                             or item["snippet"].get("publishedAt"))
                try:
                    transcript = _transcript_text(vid)
                    if not transcript:
                        raise ValueError("empty transcript")
                    brief = _summarise(client, transcript)
                except Exception as e:
                    fc.log(f"summaries: skip {channel} {vid}: {e}")
                    continue

                feed.append({
                    "channel": channel,
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "video_id": vid,
                    "published_at": published,
                    "headline": brief["headline"],
                    "summary": brief["summary"],
                    "key_points": brief["key_points"],
                    "generated_at": fc.now_iso(),
                })
                _mark_processed(season, vid)
                processed.add(vid)
                new_items += 1
                fc.log(f"summaries: + {channel} - {title}")
        except Exception as e:
            fc.log(f"summaries: error on {channel}: {e}")

    feed.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    feed = feed[:FEED_LIMIT]
    fc.write_json(feed_path, {"updated_at": fc.now_iso(), "items": feed})
    fc.log(f"summaries: {new_items} new; feed now holds {len(feed)} items.")


if __name__ == "__main__":
    run()
