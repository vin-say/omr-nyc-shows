"""
Artist enrichment module using Perplexity Sonar API.

For each eligible artist, queries Sonar to produce a Markdown report covering:
  - Genre(s) associated with the artist
  - Whether the artist has been discussed or featured in:
    Pitchfork, Brooklyn Vegan, Bandcamp Daily, Resident Advisor, KEXP

Eligibility is determined by the venue's capacity tier and the artist's
position on the bill (bill_order in show_artists):
  - Small / Small-Medium venues: headliner only (bill_order = 1)
  - Medium venues:               bill_order 1 or 2
  - Large / Major venues:        all artists on the bill
  - Unknown / NULL tier:          all artists (always enrich)

The final Markdown report is stored in artists.report and includes a
Sources section built from the Perplexity response's citations array.
"""

import sqlite3
import json
import time
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from dotenv import load_dotenv
from curl_cffi import requests as cffi_requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = "sonar"
SEARCH_CONTEXT_SIZE = "medium"
DELAY_BETWEEN_CALLS = 1  # seconds between API calls

PUBLICATIONS = [
    "Pitchfork",
    "Brooklyn Vegan",
    "Bandcamp Daily",
    "Resident Advisor",
    "KEXP",
]


def _load_perplexity_key() -> str:
    """Load the Perplexity API key from the .env file."""
    load_dotenv()
    key = os.getenv("PERPLEXITY_API_KEY")
    if not key:
        raise RuntimeError(
            "PERPLEXITY_API_KEY not found. "
            "Make sure it is set in the .env file in the project root."
        )
    return key.strip()


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
def _build_prompt(artist_name: str) -> str:
    pubs_list = ", ".join(PUBLICATIONS)
    return (
        f'Research the musical artist or band "{artist_name}".\n\n'
        f"Provide a short Markdown-formatted report covering:\n\n"
        f"1. **Genres**: List the genre(s) most commonly associated with this "
        f"artist. Be specific (e.g. \"post-punk\" rather than just \"rock\").\n\n"
        f"2. **Media Coverage**: For each of the following publications, state "
        f"whether the artist has been discussed, reviewed, or featured there. "
        f"Include the context of the coverage if available (e.g. album review, "
        f"interview, live session, playlist feature):\n"
        f"   - {pubs_list}\n\n"
        f"Use inline citation numbers (e.g. [1], [2]) to reference your sources. "
        f"If you cannot find evidence of coverage in a particular publication, "
        f"say so explicitly — do not guess."
    )


# ---------------------------------------------------------------------------
# Calling Perplexity
# ---------------------------------------------------------------------------
def _call_perplexity(artist_name: str, api_key: str) -> tuple[str, list[str]] | None:
    """
    Call Perplexity Sonar for a single artist.

    Returns (content_markdown, citations_list) on success, or None on failure.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": PERPLEXITY_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a music research assistant. Return well-structured "
                    "Markdown. Use inline citation numbers like [1], [2] to "
                    "reference your sources. Be concise but thorough."
                ),
            },
            {
                "role": "user",
                "content": _build_prompt(artist_name),
            },
        ],
        "web_search_options": {
            "search_context_size": SEARCH_CONTEXT_SIZE,
        },
    }

    def _do_request():
        return cffi_requests.post(
            PERPLEXITY_API_URL,
            headers=headers,
            json=payload,
            timeout=30,
        )

    # Use a thread-level timeout to guard against curl_cffi hanging
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_do_request)
            resp = future.result(timeout=45)
        resp.raise_for_status()
    except FuturesTimeoutError:
        print(f"   [!] Request timed out for '{artist_name}' (45s hard limit)")
        return None
    except Exception as e:
        print(f"   [!] HTTP error for '{artist_name}': {e}")
        return None

    try:
        body = resp.json()
    except Exception as e:
        print(f"   [!] Failed to decode API response for '{artist_name}': {e}")
        return None

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        print(f"   [!] Unexpected API response structure for '{artist_name}': {e}")
        return None

    citations = body.get("citations", [])

    return content, citations


def _build_report(content: str, citations: list[str]) -> str:
    """
    Combine the Perplexity Markdown content with a Sources section that
    maps the inline [1], [2], … references to their actual URLs.
    """
    if not citations:
        return content

    sources_section = "\n\n---\n\n## Sources\n\n"
    for i, url in enumerate(citations, 1):
        sources_section += f"{i}. {url}\n"

    return content + sources_section


# ---------------------------------------------------------------------------
# Eligibility query
# ---------------------------------------------------------------------------
ELIGIBLE_ARTISTS_SQL = """
    SELECT DISTINCT a.id, a.name
    FROM artists a
    JOIN show_artists sa ON sa.artist_id = a.id
    JOIN shows s ON s.id = sa.show_id
    JOIN venues v ON v.id = s.venue_id
    WHERE a.enrichment_timestamp IS NULL
      AND (
          -- Large / Major: always enrich
          v.capacity_tier IN ('Large', 'Major')
          -- Medium: headliner or second on the bill
          OR (v.capacity_tier = 'Medium' AND sa.bill_order <= 2)
          -- Small / Small-Medium: headliner only
          OR (v.capacity_tier IN ('Small', 'Small-Medium') AND sa.bill_order = 1)
          -- Unknown / NULL / N/A: always enrich
          OR v.capacity_tier IS NULL
          OR v.capacity_tier IN ('Unknown', 'N/A')
      )
    ORDER BY a.id
"""


def enrich_unenriched_artists(conn: sqlite3.Connection):
    """
    Find all eligible, unenriched artists and enrich them via Perplexity Sonar.

    Commits after each successful enrichment so progress is never lost.
    """
    cursor = conn.cursor()
    cursor.execute(ELIGIBLE_ARTISTS_SQL)
    unenriched = cursor.fetchall()

    if not unenriched:
        print("All eligible artists are already enriched. Nothing to do.")
        return

    api_key = _load_perplexity_key()

    total = len(unenriched)
    success_count = 0
    fail_count = 0

    print(f"Found {total} eligible unenriched artist(s). Starting enrichment...\n")

    for idx, (artist_id, artist_name) in enumerate(unenriched, 1):
        print(f"[{idx}/{total}] Enriching: {artist_name}...")

        if idx > 1:
            time.sleep(DELAY_BETWEEN_CALLS)

        result = _call_perplexity(artist_name, api_key)

        if result is None:
            print(f"   -> FAILED (will retry on next run)\n")
            fail_count += 1
            continue

        content, citations = result
        report = _build_report(content, citations)
        timestamp = datetime.now(timezone.utc).isoformat()

        cursor.execute(
            """
            UPDATE artists
            SET report = ?,
                enrichment_timestamp = ?
            WHERE id = ?
            """,
            (report, timestamp, artist_id),
        )
        conn.commit()

        # Display a preview of the first line of the report
        first_line = content.split("\n")[0].strip()
        preview = first_line[:80] + ("…" if len(first_line) > 80 else "")
        print(f"   -> {preview}")
        print(f"   -> {len(citations)} citation(s)")
        print(f"   -> ENRICHED OK\n")
        success_count += 1

    print("=" * 50)
    print("--- ARTIST ENRICHMENT SUMMARY ---")
    print(f"Eligible unenriched artists found: {total}")
    print(f"Successfully enriched: {success_count}")
    print(f"Failed (will retry next run): {fail_count}")
    print("=" * 50)


if __name__ == "__main__":
    """Standalone execution: enrich all eligible unenriched artists."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from scraper import init_db

    db_name = os.path.join(os.path.dirname(__file__), "concerts.db")
    conn = init_db(db_name)

    print("=" * 50)
    print("Artist Enrichment — Standalone Mode")
    print("=" * 50 + "\n")

    enrich_unenriched_artists(conn)
    conn.close()
