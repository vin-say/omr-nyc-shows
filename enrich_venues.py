"""
Venue enrichment module using Perplexity Sonar Pro API.

Looks up each unenriched venue in the SQLite database and populates:
  - address
  - is_primary_music_venue (0/1)
  - venue_type_notes
  - capacity_tier (Small / Small-Medium / Medium / Large / Major / Unknown)
  - enrichment_timestamp

Can be run standalone to backfill existing venues, or called from scraper.py
after each scrape to enrich newly added venues.
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
PERPLEXITY_MODEL = "sonar-pro"
SEARCH_CONTEXT_SIZE = "medium"
DELAY_BETWEEN_CALLS = 1  # seconds between Perplexity API calls


def load_perplexity_key():
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
# JSON schema sent to Perplexity for structured output
# ---------------------------------------------------------------------------
VENUE_JSON_SCHEMA = {
    "name": "venue_info",
    "schema": {
        "type": "object",
        "properties": {
            "address": {
                "type": ["string", "null"],
                "description": "Full street address of the venue, or null if unknown."
            },
            "is_primary_music_venue": {
                "type": ["boolean", "null"],
                "description": (
                    "true if live music is the primary purpose of this venue "
                    "(e.g. dedicated concert hall, nightclub, amphitheater). "
                    "false if live music is secondary (e.g. bar, restaurant, "
                    "event space that occasionally hosts shows). null if unknown."
                )
            },
            "venue_type_notes": {
                "type": ["string", "null"],
                "description": (
                    "Brief description of the venue type, e.g. "
                    "'dedicated concert hall', 'outdoor amphitheater', "
                    "'bar with live music stage'. null if unknown."
                )
            },
            "capacity_tier": {
                "type": ["string", "null"],
                "enum": ["Small", "Small-Medium", "Medium", "Large", "Major", "Unknown", None],
                "description": (
                    "Attendee capacity tier: "
                    "'Small' (<100), 'Small-Medium' (100-499), "
                    "'Medium' (500-1499), 'Large' (1500-4999), "
                    "'Major' (5000+), 'Unknown' if no estimate possible. "
                    "null if cannot be determined at all."
                )
            }
        },
        "required": [
            "address",
            "is_primary_music_venue",
            "venue_type_notes",
            "capacity_tier"
        ],
        "additionalProperties": False
    }
}


def _build_prompt(venue_name: str) -> str:
    """Build the user prompt for a single venue lookup."""
    return (
        f'Look up the live music venue "{venue_name}" located in New York City.\n'
        f"Return the following information as a JSON object:\n\n"
        f"1. **address**: Full street address of the venue.\n"
        f"2. **is_primary_music_venue**: Is live music the primary purpose of this "
        f"venue (e.g. dedicated concert hall, nightclub, amphitheater)? Set to true. "
        f"If live music is secondary (e.g. bar, restaurant, event space that "
        f"occasionally hosts shows), set to false.\n"
        f"3. **venue_type_notes**: Briefly describe the venue type.\n"
        f"4. **capacity_tier**: Classify into one of these attendee capacity tiers: "
        f'"Small" (<100), "Small-Medium" (100–499), "Medium" (500–1,499), '
        f'"Large" (1,500–4,999), "Major" (5,000+). '
        f'If no estimate is possible, use "Unknown".\n\n'
        f"Do not guess or hallucinate. If you cannot confidently determine a field, "
        f"use null."
    )


def enrich_venue(venue_name: str, api_key: str) -> dict | None:
    """
    Call Perplexity Sonar Pro to look up metadata for a single venue.

    Returns a dict with keys: address, is_primary_music_venue,
    venue_type_notes, capacity_tier.  Returns None on any failure.
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
                    "You are a research assistant that returns structured JSON "
                    "about live music venues in New York City. "
                    "Always respond with the requested JSON schema. "
                    "Do not include any extra text outside the JSON object."
                ),
            },
            {
                "role": "user",
                "content": _build_prompt(venue_name),
            },
        ],
        "web_search_options": {
            "search_context_size": SEARCH_CONTEXT_SIZE,
        },
        "response_format": {
            "type": "json_schema",
            "json_schema": VENUE_JSON_SCHEMA,
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
        print(f"   [!] Request timed out for '{venue_name}' (45s hard limit)")
        return None
    except Exception as e:
        print(f"   [!] HTTP error for '{venue_name}': {e}")
        return None

    try:
        body = resp.json()
    except Exception as e:
        print(f"   [!] Failed to decode API response for '{venue_name}': {e}")
        return None

    # Extract the assistant message content
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        print(f"   [!] Unexpected API response structure for '{venue_name}': {e}")
        return None

    # Parse the structured JSON from the content string
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"   [!] Invalid JSON in API response for '{venue_name}': {e}")
        print(f"       Raw content: {content[:200]}")
        return None

    # Validate expected keys are present
    expected_keys = {"address", "is_primary_music_venue", "venue_type_notes", "capacity_tier"}
    if not expected_keys.issubset(data.keys()):
        missing = expected_keys - data.keys()
        print(f"   [!] Missing keys in response for '{venue_name}': {missing}")
        return None

    # Validate capacity_tier value
    valid_tiers = {"Small", "Small-Medium", "Medium", "Large", "Major", "Unknown", None}
    if data.get("capacity_tier") not in valid_tiers:
        print(f"   [!] Invalid capacity_tier '{data.get('capacity_tier')}' for '{venue_name}', setting to Unknown.")
        data["capacity_tier"] = "Unknown"

    return data


def enrich_unenriched_venues(conn: sqlite3.Connection):
    """
    Find all venues without enrichment data and enrich them via Perplexity.

    Commits after each successful enrichment so progress is never lost.
    """
    cursor = conn.cursor()

    # Find venues that haven't been enriched yet
    cursor.execute(
        "SELECT id, name FROM venues WHERE enrichment_timestamp IS NULL ORDER BY id"
    )
    unenriched = cursor.fetchall()

    if not unenriched:
        print("All venues are already enriched. Nothing to do.")
        return

    api_key = load_perplexity_key()

    total = len(unenriched)
    success_count = 0
    fail_count = 0

    print(f"Found {total} unenriched venue(s). Starting enrichment...\n")

    for idx, (venue_id, venue_name) in enumerate(unenriched, 1):
        print(f"[{idx}/{total}] Enriching: {venue_name}...")

        # Rate limiting
        if idx > 1:
            time.sleep(DELAY_BETWEEN_CALLS)

        result = enrich_venue(venue_name, api_key)

        if result is None:
            print(f"   -> FAILED (will retry on next run)\n")
            fail_count += 1
            continue

        # Convert boolean to integer for SQLite
        is_primary = None
        if result.get("is_primary_music_venue") is not None:
            is_primary = 1 if result["is_primary_music_venue"] else 0

        timestamp = datetime.now(timezone.utc).isoformat()

        cursor.execute(
            """
            UPDATE venues
            SET address = ?,
                is_primary_music_venue = ?,
                venue_type_notes = ?,
                capacity_tier = ?,
                enrichment_timestamp = ?
            WHERE id = ?
            """,
            (
                result.get("address"),
                is_primary,
                result.get("venue_type_notes"),
                result.get("capacity_tier"),
                timestamp,
                venue_id,
            ),
        )
        conn.commit()

        addr_display = result.get("address") or "N/A"
        tier_display = result.get("capacity_tier") or "N/A"
        type_display = result.get("venue_type_notes") or "N/A"
        primary_display = "Yes" if is_primary else ("No" if is_primary == 0 else "N/A")

        print(f"   -> Address: {addr_display}")
        print(f"   -> Primary music venue: {primary_display}")
        print(f"   -> Type: {type_display}")
        print(f"   -> Capacity tier: {tier_display}")
        print(f"   -> ENRICHED OK\n")
        success_count += 1

    print("=" * 50)
    print("--- VENUE ENRICHMENT SUMMARY ---")
    print(f"Total unenriched venues found: {total}")
    print(f"Successfully enriched: {success_count}")
    print(f"Failed (will retry next run): {fail_count}")
    print("=" * 50)


if __name__ == "__main__":
    """Standalone execution: enrich all unenriched venues in the database."""
    from scraper import init_db

    db_name = "concerts.db"
    conn = init_db(db_name)

    print("=" * 50)
    print("Venue Enrichment - Standalone Mode")
    print("=" * 50 + "\n")

    enrich_unenriched_venues(conn)
    conn.close()
