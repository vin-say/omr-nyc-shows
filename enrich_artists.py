"""
Artist enrichment module using Perplexity Sonar API.

For each eligible artist, queries Sonar to produce a structured JSON report
covering:
  - Genre(s) associated with the artist
  - Whether the artist has been discussed or featured in:
    Pitchfork, Brooklyn Vegan, Bandcamp Daily, Resident Advisor, KEXP

Eligibility is determined by the venue's capacity tier and the artist's
position on the bill (bill_order in show_artists):
  - Small / Small-Medium venues: headliner only (bill_order = 1)
  - Medium venues:               bill_order 1 or 2
  - Large / Major venues:        all artists on the bill
  - Unknown / NULL tier:          all artists (always enrich)

The structured JSON is stored in artists.coverage_json.  The Perplexity
citations array is embedded in the JSON so the newsletter renderer can
produce inline hyperlinks.
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
SEARCH_CONTEXT_SIZE = "high"
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
# JSON schema for structured output
# ---------------------------------------------------------------------------
ARTIST_JSON_SCHEMA = {
    "name": "artist_info",
    "schema": {
        "type": "object",
        "properties": {
            "genres": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Specific genres associated with this artist, e.g. "
                    "'post-punk', 'ambient jazz', 'indie folk'. "
                    "Be specific rather than broad."
                ),
            },
            "coverage": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "publication": {
                            "type": "string",
                            "description": "Name of the publication.",
                        },
                        "covered": {
                            "type": "boolean",
                            "description": (
                                "true if you found evidence that this publication "
                                "has discussed, reviewed, or featured the artist. "
                                "false if you found no such evidence."
                            ),
                        },
                        "context": {
                            "type": ["string", "null"],
                            "description": (
                                "Brief description of the coverage if covered is true "
                                "(e.g. 'album review of Punisher', 'live in-studio session', "
                                "'interview about upcoming tour'). null if covered is false."
                            ),
                        },
                        "citation_index": {
                            "type": ["integer", "null"],
                            "description": (
                                "The 1-based citation number that supports this coverage claim "
                                "(e.g. 1 for [1], 2 for [2]). null if covered is false or "
                                "no specific citation applies."
                            ),
                        },
                    },
                    "required": ["publication", "covered", "context", "citation_index"],
                    "additionalProperties": False,
                },
                "description": "Coverage status for each of the requested publications.",
            },
        },
        "required": ["genres", "coverage"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
def _build_prompt(artist_name: str) -> str:
    pubs_list = ", ".join(PUBLICATIONS)
    return (
        f'Research the musical artist or band "{artist_name}".\n\n'
        f"Provide a JSON object with two fields:\n\n"
        f"1. **genres**: An array of specific genre labels most commonly "
        f"associated with this artist (e.g. \"post-punk\" rather than just "
        f"\"rock\"). Include 2-5 genres.\n\n"
        f"2. **coverage**: For each of the following publications, determine "
        f"whether the artist has been discussed, reviewed, or featured:\n"
        f"   {pubs_list}\n\n"
        f"   You may set covered=true based on EITHER:\n"
        f"   - Direct evidence from search results (preferred — include the "
        f"citation_index if available)\n"
        f"   - Your training knowledge that the publication has covered this "
        f"artist (e.g. you know Pitchfork reviewed their album, even if the "
        f"specific URL wasn't in search results)\n\n"
        f"   Only set covered=false if you genuinely have no knowledge or "
        f"evidence of coverage. When in doubt for well-known artists, lean "
        f"toward true. For obscure/unknown artists, lean toward false.\n\n"
        f"   When covered=true, provide context describing the type of coverage "
        f"(album review, interview, live session, etc.). Set citation_index to "
        f"the relevant source number if one exists, or null if based on general "
        f"knowledge."
    )


# ---------------------------------------------------------------------------
# Calling Perplexity
# ---------------------------------------------------------------------------
def _call_perplexity(artist_name: str, api_key: str) -> dict | None:
    """
    Call Perplexity Sonar for a single artist with structured JSON output.

    Returns a dict with keys: genres, coverage, citations.
    Returns None on failure.
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
                    "You are a music research assistant. Return structured JSON "
                    "about musical artists. Use both your search results AND your "
                    "training knowledge to determine publication coverage. "
                    "If you know from training data that a major publication like "
                    "Pitchfork or KEXP has covered an artist, report that as "
                    "covered=true even if the specific URL isn't in search results."
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
        "response_format": {
            "type": "json_schema",
            "json_schema": ARTIST_JSON_SCHEMA,
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

    # Extract the structured JSON content
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        print(f"   [!] Unexpected API response structure for '{artist_name}': {e}")
        return None

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"   [!] Invalid JSON in API response for '{artist_name}': {e}")
        print(f"       Raw content: {content[:200]}")
        return None

    # Validate expected keys
    if "genres" not in data or "coverage" not in data:
        print(f"   [!] Missing required keys in response for '{artist_name}'")
        return None

    # Attach the citations array from the Perplexity response
    citations = body.get("citations", [])
    data["citations"] = citations

    return data


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

        coverage_json = json.dumps(result, ensure_ascii=False)
        timestamp = datetime.now(timezone.utc).isoformat()

        cursor.execute(
            """
            UPDATE artists
            SET coverage_json = ?,
                enrichment_timestamp = ?
            WHERE id = ?
            """,
            (coverage_json, timestamp, artist_id),
        )
        conn.commit()

        # Display a preview
        genres = ", ".join(result.get("genres", []))
        covered_pubs = [
            c["publication"]
            for c in result.get("coverage", [])
            if c.get("covered")
        ]
        covered_str = ", ".join(covered_pubs) if covered_pubs else "none"
        print(f"   -> Genres: {genres}")
        print(f"   -> Coverage: {covered_str}")
        print(f"   -> {len(result.get('citations', []))} citation(s)")
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
