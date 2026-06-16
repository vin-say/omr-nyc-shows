"""
Artist link enrichment using Spotify and YouTube APIs.

Adds a Spotify artist URL and a YouTube live performance video URL to each
artist that doesn't already have them.

YouTube has a 100-search/day quota, so artists are prioritized:
  1. Artists with media coverage OR at a priority venue (tiers 1-3)
  2. Remaining artists, using whatever quota is left

Spotify has generous rate limits and enriches all eligible artists.

Both respect the venue blacklist — artists whose only shows are at
blacklisted venues are skipped.
"""

import sqlite3
import json
import time
import os
import base64
from datetime import datetime, timezone

import yaml
from dotenv import load_dotenv
from curl_cffi import requests as cffi_requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PRIORITY_VENUES_FILE = os.path.join(SCRIPT_DIR, "priority_venues.yaml")

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_DAILY_QUOTA = 100

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SEARCH_URL = "https://api.spotify.com/v1/search"

DELAY_BETWEEN_CALLS = 0.5  # seconds between API calls


def _load_env():
    load_dotenv()
    return {
        "youtube_api_key": os.getenv("YOUTUBE_API_KEY", "").strip(),
        "spotify_client_id": os.getenv("SPOTIFY_CLIENT_ID", "").strip(),
        "spotify_client_secret": os.getenv("SPOTIFY_CLIENT_SECRET", "").strip(),
    }


def _load_venues_config(path: str = PRIORITY_VENUES_FILE) -> tuple[set[str], set[str]]:
    """Returns (priority_venues, blacklisted_venues) as lowercase sets."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    priority = {v.strip().lower() for v in data.get("priority_venues", [])}
    blacklisted = {v.strip().lower() for v in data.get("blacklisted_venues", [])}
    return priority, blacklisted


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------
def _get_spotify_token(client_id: str, client_secret: str) -> str | None:
    """Get a Spotify access token via client credentials flow."""
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        resp = cffi_requests.post(
            SPOTIFY_TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data="grant_type=client_credentials",
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as e:
        print(f"[!] Failed to get Spotify token: {e}")
        return None


def _search_spotify(artist_name: str, token: str) -> str | None:
    """Search Spotify for an artist and return their Spotify URL."""
    try:
        resp = cffi_requests.get(
            SPOTIFY_SEARCH_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"q": artist_name, "type": "artist", "limit": 1},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("artists", {}).get("items", [])
        if items:
            return items[0].get("external_urls", {}).get("spotify")
    except Exception as e:
        print(f"   [!] Spotify search failed for '{artist_name}': {e}")
    return None


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------
def _search_youtube(artist_name: str, api_key: str) -> str | None:
    """Search YouTube for an artist live performance and return the video URL."""
    try:
        resp = cffi_requests.get(
            YOUTUBE_SEARCH_URL,
            params={
                "part": "snippet",
                "q": f"{artist_name} live performance",
                "type": "video",
                "maxResults": 1,
                "key": api_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if items:
            video_id = items[0]["id"].get("videoId")
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"
    except Exception as e:
        print(f"   [!] YouTube search failed for '{artist_name}': {e}")
    return None


# ---------------------------------------------------------------------------
# Eligibility queries
# ---------------------------------------------------------------------------
def _build_eligible_query(blacklisted: set[str], column: str) -> tuple[str, list[str]]:
    """
    Build a query for artists missing a specific link column,
    excluding artists whose only shows are at blacklisted venues.
    """
    if blacklisted:
        placeholders = ",".join("?" * len(blacklisted))
        blacklist_clause = f"AND LOWER(TRIM(v.name)) NOT IN ({placeholders})"
        params = list(blacklisted)
    else:
        blacklist_clause = ""
        params = []

    sql = f"""
        SELECT DISTINCT a.id, a.name, a.coverage_json,
               GROUP_CONCAT(DISTINCT LOWER(TRIM(v.name))) AS venue_names
        FROM artists a
        JOIN show_artists sa ON sa.artist_id = a.id
        JOIN shows s ON s.id = sa.show_id
        JOIN venues v ON v.id = s.venue_id
        WHERE a.{column} IS NULL
          {blacklist_clause}
        GROUP BY a.id
        ORDER BY a.id
    """
    return sql, params


def _has_media_coverage(coverage_json: str | None) -> bool:
    if not coverage_json:
        return False
    try:
        data = json.loads(coverage_json)
        return any(c.get("covered") for c in data.get("coverage", []))
    except (json.JSONDecodeError, TypeError):
        return False


def _is_priority_venue_artist(venue_names: str | None, priority_venues: set[str]) -> bool:
    if not venue_names:
        return False
    return any(v.strip() in priority_venues for v in venue_names.split(","))


# ---------------------------------------------------------------------------
# Main enrichment functions
# ---------------------------------------------------------------------------
def enrich_spotify(conn: sqlite3.Connection):
    """Enrich all eligible artists with Spotify URLs."""
    env = _load_env()
    if not env["spotify_client_id"] or not env["spotify_client_secret"]:
        print("Spotify credentials not configured. Skipping Spotify enrichment.")
        return

    _, blacklisted = _load_venues_config()
    sql, params = _build_eligible_query(blacklisted, "spotify_url")

    cursor = conn.cursor()
    cursor.execute(sql, params)
    artists = cursor.fetchall()

    if not artists:
        print("All eligible artists already have Spotify URLs.")
        return

    token = _get_spotify_token(env["spotify_client_id"], env["spotify_client_secret"])
    if not token:
        return

    total = len(artists)
    found = 0
    print(f"\nSpotify: {total} artist(s) to look up...\n")

    for idx, (artist_id, artist_name, _, _) in enumerate(artists, 1):
        if idx > 1:
            time.sleep(DELAY_BETWEEN_CALLS)

        url = _search_spotify(artist_name, token)
        if url:
            cursor.execute("UPDATE artists SET spotify_url = ? WHERE id = ?", (url, artist_id))
            conn.commit()
            print(f"  [{idx}/{total}] {artist_name} -> {url}")
            found += 1
        else:
            print(f"  [{idx}/{total}] {artist_name} -> not found")

    print(f"\nSpotify enrichment complete: {found}/{total} artists matched.\n")


def enrich_youtube(conn: sqlite3.Connection):
    """
    Enrich artists with YouTube URLs, respecting daily quota.

    Priority order:
      1. Artists with media coverage or at a priority venue
      2. Remaining artists (with leftover quota)
    """
    env = _load_env()
    if not env["youtube_api_key"]:
        print("YouTube API key not configured. Skipping YouTube enrichment.")
        return

    priority_venues, blacklisted = _load_venues_config()
    sql, params = _build_eligible_query(blacklisted, "youtube_url")

    cursor = conn.cursor()
    cursor.execute(sql, params)
    all_artists = cursor.fetchall()

    if not all_artists:
        print("All eligible artists already have YouTube URLs.")
        return

    # Split into priority (media coverage or priority venue) and the rest
    priority_artists = []
    other_artists = []
    for row in all_artists:
        artist_id, artist_name, coverage_json, venue_names = row
        if _has_media_coverage(coverage_json) or _is_priority_venue_artist(venue_names, priority_venues):
            priority_artists.append(row)
        else:
            other_artists.append(row)

    ordered = priority_artists + other_artists
    quota_remaining = YOUTUBE_DAILY_QUOTA
    total = len(ordered)
    found = 0

    print(f"\nYouTube: {len(priority_artists)} priority + {len(other_artists)} other artist(s) "
          f"(quota: {YOUTUBE_DAILY_QUOTA}/day)...\n")

    for idx, (artist_id, artist_name, _, _) in enumerate(ordered, 1):
        if quota_remaining <= 0:
            print(f"  YouTube daily quota reached ({YOUTUBE_DAILY_QUOTA}). "
                  f"Remaining {total - idx + 1} artist(s) will be enriched tomorrow.")
            break

        if idx > 1:
            time.sleep(DELAY_BETWEEN_CALLS)

        url = _search_youtube(artist_name, env["youtube_api_key"])
        quota_remaining -= 1

        if url:
            cursor.execute("UPDATE artists SET youtube_url = ? WHERE id = ?", (url, artist_id))
            conn.commit()
            print(f"  [{idx}/{total}] {artist_name} -> {url}")
            found += 1
        else:
            print(f"  [{idx}/{total}] {artist_name} -> not found")

    print(f"\nYouTube enrichment complete: {found} matched, "
          f"{quota_remaining} quota remaining.\n")


def enrich_links(conn: sqlite3.Connection):
    """Run both Spotify and YouTube enrichment."""
    print("=" * 50)
    print("Link Enrichment (Spotify + YouTube)")
    print("=" * 50)
    enrich_spotify(conn)
    enrich_youtube(conn)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from scraper import init_db

    db_path = os.path.join(SCRIPT_DIR, "concerts.db")
    conn = init_db(db_path)
    enrich_links(conn)
    conn.close()
