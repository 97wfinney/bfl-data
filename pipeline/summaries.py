"""summaries.py - summarise the latest FPL YouTube uploads into a news feed.

For each channel it checks the latest few uploads, summarises any not seen
before with gpt-5.5 (Responses API), and maintains a rolling, newest-first
feed capped at FEED_LIMIT items at:

  data/<season>/summaries/feed.json

Dedup is tracked in data/<season>/state/processed_video_ids.txt.
The heavy YouTube/OpenAI imports are done lazily inside run() so a problem
here can never break the FPL data steps. No Discord.
"""
from __future__ import annotations

import os

import fpl_common as fc

CHANNELS = {
    "Let's Talk FPL": "UCxeOc7eFxq37yW_Nc-69deA",
    "FPL Focal": "UC72QokPHXQ9r98ROfNZmaDw",
    "Fantasy Football Scout": "UCKxYKQ8pgJ7V8wwh4hLsSXQ",
    "FPL Mate": "UCweDAlFm2LnVcOqaFU4_AGA",
    "FPL Harry": "UCcPWnCj5AKC19HaySZjb25g",
}

MAX_PER_CHANNEL = 3
FEED_LIMIT = 15

SYSTEM_PROMPT = (
    "You are a wire editor writing Fantasy Premier League briefings for a "
    "terminal news feed. From the transcript, pull only what's actionable for "
    "an FPL manager: captaincy, transfers in/out, differentials, injuries and "
    "price moves, chip timing, fixture swings. Style is clipped wire copy - "
    "declarative, present tense, players named, no hedging, no 'the video says', "
    "no intro or outro. Output a single one-line headline take, then 3-6 terse "
    "bullets. Drop anything that isn't a concrete call. Keep it under ~130 words."
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


def _summarise(client, transcript):
    resp = client.responses.create(
        model="gpt-5.5",
        instructions=SYSTEM_PROMPT,
        input=transcript,
        max_output_tokens=1000,
    )
    return resp.output_text.strip()


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
                    summary = _summarise(client, transcript)
                except Exception as e:
                    fc.log(f"summaries: skip {channel} {vid}: {e}")
                    continue

                feed.append({
                    "channel": channel,
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "video_id": vid,
                    "published_at": published,
                    "summary": summary,
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
