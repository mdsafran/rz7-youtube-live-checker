#!/usr/bin/env python3
"""
RZ7 YouTube Live Detection — yt-dlp based checker.

Uses yt-dlp to check each channel's /streams tab for live broadcasts.
This avoids the YouTube Data API v3 quota system entirely.

Approach:
  1. Fetch the channel's /streams tab with flat extraction (lightweight).
     - If the first entry has live_status == "is_live", the channel is LIVE.
     - If all entries are "was_live" or "is_upcoming", the channel is OFFLINE.
  2. For LIVE channels, attempt a deep extraction of the live video to get
     viewer count and thumbnail. If deep extraction fails (bot detection),
     fall back to the flat-extracted title + constructed thumbnail URL.

Key principle: NEVER mark a member OFFLINE just because a request failed.
On failure or unclear results, report UNKNOWN (leave existing status unchanged).

Usage:
  python check_youtube_live.py                        # check all YouTube members
  python check_youtube_live.py --slug bhoomika-valli  # check one member
  python check_youtube_live.py --limit 3              # check first N members
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests
import yt_dlp

# ── Supabase config from environment ────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# ── Constants ───────────────────────────────────────────────────
CHECKED_BY = "ytdlp"
REQUEST_TIMEOUT = 30
INTER_MEMBER_DELAY = 2

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def build_streams_url(channel_url: str) -> str:
    """Build the /streams URL for a YouTube channel handle."""
    url = channel_url.rstrip("/")
    if url.endswith("/streams"):
        return url
    if url.endswith("/live"):
        url = url[:-len("/live")]
    return url + "/streams"


def check_streams_tab(streams_url: str) -> dict:
    """
    Check a channel's /streams tab using flat extraction.

    Returns a dict with:
      status: "LIVE" | "OFFLINE" | "UNKNOWN"
      video_id: str or None
      title: str or None
      error: str or None
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "http_headers": BROWSER_HEADERS,
        "socket_timeout": REQUEST_TIMEOUT,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(streams_url, download=False)

        if info is None:
            return {"status": "UNKNOWN", "video_id": None, "title": None, "error": "no info"}

        entries = info.get("entries") or []

        # Check the first few entries for a live stream
        for entry in entries[:5]:
            if not entry:
                continue
            live_status = entry.get("live_status")
            if live_status == "is_live":
                return {
                    "status": "LIVE",
                    "video_id": entry.get("id"),
                    "title": entry.get("title"),
                    "error": None,
                }

        # No live entry found — channel is offline
        return {"status": "OFFLINE", "video_id": None, "title": None, "error": None}

    except yt_dlp.utils.DownloadError as e:
        err_str = str(e)
        # "Unable to recognize tab page" → channel genuinely has no streams tab
        if "Unable to recognize tab page" in err_str:
            return {"status": "OFFLINE", "video_id": None, "title": None, "error": None}
        log(f"  DownloadError: {err_str[:200]}")
        return {"status": "UNKNOWN", "video_id": None, "title": None, "error": err_str[:500]}

    except Exception as e:
        log(f"  Unexpected error: {type(e).__name__}: {e}")
        return {"status": "UNKNOWN", "video_id": None, "title": None, "error": f"{type(e).__name__}: {e}"}


def try_fetch_live_details(video_id: str) -> dict:
    """
    Attempt a deep extraction of a live video to get viewer count + thumbnail.
    Falls back gracefully if YouTube blocks the request.

    Returns dict with: viewers (int), thumbnail (str|None)
    """
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "http_headers": BROWSER_HEADERS,
        "socket_timeout": REQUEST_TIMEOUT,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        if info:
            viewers = info.get("concurrent_view_count") or info.get("view_count") or 0
            thumbnail = info.get("thumbnail")
            return {"viewers": int(viewers), "thumbnail": thumbnail}
    except Exception as e:
        log(f"  Deep extraction failed (non-fatal): {str(e)[:150]}")

    # Fallback: construct thumbnail from video ID, no viewer count
    return {
        "viewers": 0,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault_live.jpg",
    }


# ── Supabase API helpers ────────────────────────────────────────

def supabase_select(url: str, key: str, params: dict | None = None) -> list:
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    query = ""
    if params:
        query = "?" + "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    resp = requests.get(f"{url}/rest/v1/players{query}", headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def supabase_update(url: str, key: str, row_id: str, data: dict) -> None:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    resp = requests.patch(
        f"{url}/rest/v1/players?id=eq.{row_id}",
        headers=headers,
        json=data,
        timeout=15,
    )
    resp.raise_for_status()


# ── Main checker logic ──────────────────────────────────────────

def run_check(supabase_url: str, supabase_key: str, slug_filter: str | None = None, limit: int | None = None) -> dict:
    params = {"select": "id,slug,name,youtube_url,youtube_is_live"}
    if slug_filter:
        params["slug"] = f"eq.{slug_filter}"

    log("Fetching YouTube members from Supabase...")
    members = supabase_select(supabase_url, supabase_key, params)
    yt_members = [m for m in members if m.get("youtube_url")]
    if limit:
        yt_members = yt_members[:limit]

    log(f"Found {len(yt_members)} YouTube member(s) to check.")

    results = {"checked": 0, "live": 0, "offline": 0, "unknown": 0, "errors": 0, "members": []}
    now_iso = datetime.now(timezone.utc).isoformat()

    for member in yt_members:
        name = member["name"]
        slug = member["slug"]
        channel_url = member["youtube_url"]
        streams_url = build_streams_url(channel_url)

        log(f"Checking: {name} ({slug})")
        check = check_streams_tab(streams_url)

        results["checked"] += 1
        status = check["status"]
        results[status.lower()] += 1
        if check["error"]:
            results["errors"] += 1

        video_id = check["video_id"]
        title = check["title"]
        viewers = 0
        thumbnail = None
        video_url = None

        if status == "LIVE" and video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            log(f"  → LIVE: {title}")
            # Try to get viewer count + thumbnail via deep extraction
            details = try_fetch_live_details(video_id)
            viewers = details["viewers"]
            thumbnail = details["thumbnail"]
            if viewers:
                log(f"  → Viewers: {viewers}")
        elif status == "OFFLINE":
            log(f"  → OFFLINE")
        else:
            log(f"  → UNKNOWN (leaving existing status)")

        member_result = {
            "name": name,
            "slug": slug,
            "channel_url": channel_url,
            "status": status,
            "title": title,
            "viewers": viewers,
            "video_url": video_url,
        }
        results["members"].append(member_result)

        # ── Update Supabase ──
        if status == "UNKNOWN":
            update_data = {
                "youtube_last_checked": now_iso,
                "youtube_checked_by": CHECKED_BY,
            }
        elif status == "LIVE":
            update_data = {
                "youtube_is_live": True,
                "youtube_live_title": title,
                "youtube_viewer_count": viewers,
                "youtube_video_url": video_url,
                "youtube_thumbnail_url": thumbnail,
                "youtube_last_checked": now_iso,
                "youtube_checked_by": CHECKED_BY,
            }
        else:  # OFFLINE
            update_data = {
                "youtube_is_live": False,
                "youtube_live_title": None,
                "youtube_viewer_count": 0,
                "youtube_video_url": None,
                "youtube_thumbnail_url": None,
                "youtube_last_checked": now_iso,
                "youtube_checked_by": CHECKED_BY,
            }

        try:
            supabase_update(supabase_url, supabase_key, member["id"], update_data)
            log(f"  → DB updated ({status})")
        except Exception as e:
            log(f"  → DB update FAILED: {e}")
            member_result["db_error"] = str(e)

        if member != yt_members[-1]:
            time.sleep(INTER_MEMBER_DELAY)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="RZ7 YouTube Live Checker (yt-dlp)")
    parser.add_argument("--slug", type=str, default=None, help="Check a single member by slug")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of members to check")
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        log("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")
        return 1

    log("=" * 60)
    log("RZ7 YouTube Live Checker — yt-dlp /streams method")
    log("=" * 60)

    results = run_check(SUPABASE_URL, SUPABASE_SERVICE_KEY, args.slug, args.limit)
    print(json.dumps(results, indent=2))

    has_db_errors = any("db_error" in m for m in results["members"])
    return 1 if has_db_errors else 0


if __name__ == "__main__":
    sys.exit(main())
