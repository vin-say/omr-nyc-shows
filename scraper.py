"""
requirements.txt
curl-cffi
python-dotenv
"""

import sqlite3
from curl_cffi import requests
import time
import json
from datetime import date

# The Oh My Rockness site loads its main show listings via a JSON API.
# The auth token is publicly embedded in their JavaScript bundle.
API_BASE = "https://www.ohmyrockness.com/api/shows.json"
API_TOKEN = "3b35f8a73dabd5f14b1cac167a14c1f6"
SHOWS_PER_PAGE = 50

def init_db(db_name="concerts.db"):
    """Initialize the SQLite database with required tables and foreign keys."""
    conn = sqlite3.connect(db_name)

    conn.execute("PRAGMA foreign_keys = ON;")

    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS venues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    ''')

    enrichment_columns = [
        ("address", "TEXT"),
        ("is_primary_music_venue", "INTEGER"),
        ("venue_type_notes", "TEXT"),
        ("capacity_tier", "TEXT"),
        ("enrichment_timestamp", "TEXT"),
    ]
    for col_name, col_type in enrichment_columns:
        try:
            cursor.execute(f"ALTER TABLE venues ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS artists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            omr_id INTEGER UNIQUE,
            omr_slug TEXT,
            report TEXT,
            enrichment_timestamp TEXT
        )
    ''')

    # Add structured JSON column for artist enrichment (v2 format)
    try:
        cursor.execute("ALTER TABLE artists ADD COLUMN coverage_json TEXT")
    except sqlite3.OperationalError:
        pass

    for col_name in ("spotify_url", "youtube_url"):
        try:
            cursor.execute(f"ALTER TABLE artists ADD COLUMN {col_name} TEXT")
        except sqlite3.OperationalError:
            pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            omr_show_id INTEGER UNIQUE,
            venue_id INTEGER NOT NULL,
            show_date TEXT NOT NULL,
            announcement_date DATE DEFAULT CURRENT_DATE,
            ticket_url TEXT,
            source_url TEXT,
            FOREIGN KEY(venue_id) REFERENCES venues(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS show_artists (
            show_id INTEGER NOT NULL,
            artist_id INTEGER NOT NULL,
            bill_order INTEGER,
            PRIMARY KEY (show_id, artist_id),
            FOREIGN KEY(show_id) REFERENCES shows(id),
            FOREIGN KEY(artist_id) REFERENCES artists(id)
        )
    ''')

    conn.commit()
    return conn

def get_or_create_artist(cursor, name, omr_id=None, omr_slug=None):
    """Retrieve an artist's ID by OMR id (preferred) or name, or insert a new one."""
    if omr_id is not None:
        cursor.execute("SELECT id FROM artists WHERE omr_id = ?", (omr_id,))
        row = cursor.fetchone()
        if row:
            return row[0]

    cursor.execute("SELECT id FROM artists WHERE name = ?", (name,))
    row = cursor.fetchone()
    if row:
        return row[0]

    cursor.execute(
        "INSERT INTO artists (name, omr_id, omr_slug) VALUES (?, ?, ?)",
        (name, omr_id, omr_slug),
    )
    return cursor.lastrowid

def get_or_create_venue(cursor, name):
    """Retrieve a venue's ID or insert a new one if it doesn't exist."""
    cursor.execute("SELECT id FROM venues WHERE name = ?", (name,))
    row = cursor.fetchone()
    if row:
        return row[0]
    else:
        cursor.execute("INSERT INTO venues (name) VALUES (?)", (name,))
        return cursor.lastrowid

def insert_show(cursor, omr_show_id, venue_id, show_date, ticket_url, source_url):
    """Insert a show, ignoring duplicates based on omr_show_id UNIQUE constraint."""
    cursor.execute('''
        INSERT OR IGNORE INTO shows
        (omr_show_id, venue_id, show_date, ticket_url, source_url)
        VALUES (?, ?, ?, ?, ?)
    ''', (omr_show_id, venue_id, show_date, ticket_url, source_url))
    return cursor.rowcount


def link_artist_to_show(cursor, show_id, artist_id, bill_order):
    """Link an artist to a show in the junction table."""
    cursor.execute('''
        INSERT OR IGNORE INTO show_artists (show_id, artist_id, bill_order)
        VALUES (?, ?, ?)
    ''', (show_id, artist_id, bill_order))

def scrape_oh_my_rockness():
    """
    Scrape newly announced shows from Oh My Rockness.
    
    Uses the site's JSON API (the same endpoint the main content column
    lazy-loads from) rather than parsing sidebar HTML. This targets the
    correct "Just Announced" listing from the central content area.
    """
    page = 1
    shows_data = []
    
    # Auth header required by the API (token is publicly embedded in their JS bundle)
    headers = {
        "Authorization": f'Token token="{API_TOKEN}"'
    }
    
    # Construct the source URL for the human-readable page
    source_page_url = "https://www.ohmyrockness.com/shows/just-announced"

    while True:
        api_url = f"{API_BASE}?just_announced=true&page={page}&per={SHOWS_PER_PAGE}&regioned=1"
        display_url = source_page_url if page == 1 else f"{source_page_url}?page={page}"
        
        print(f"[Page {page}] Fetching from API ({api_url})...")
        
        # Be gentle: Wait 3 seconds between requests
        time.sleep(3)
        
        try:
            # Impersonate Chrome to bypass Cloudflare protection
            response = requests.get(api_url, headers=headers, impersonate="chrome124", timeout=15)
            response.raise_for_status()
        except Exception as e:
            print(f" -> ERROR: Failed to fetch page {page}: {e}")
            break
        
        try:
            page_shows = response.json()
        except Exception as e:
            print(f" -> ERROR: Failed to parse JSON response: {e}")
            break
        
        # The API returns an empty list when there are no more pages
        if not isinstance(page_shows, list) or len(page_shows) == 0:
            print(f" -> No more shows found. Reached end of listings.")
            break
            
        print(f" -> Success! Retrieved {len(page_shows)} shows from page {page}.")
        
        for show in page_shows:
            omr_show_id = show.get('id')

            bands = show.get('cached_bands', [])

            venue = show.get('venue', {}).get('name', 'Unknown Venue')

            starts_at = show.get('starts_at', '')
            show_date = starts_at.split('T')[0] if 'T' in starts_at else starts_at
            if not show_date:
                show_date = 'Unknown Date'

            ticket_url = show.get('tickets_url')

            shows_data.append({
                "omr_show_id": omr_show_id,
                "bands": bands,
                "venue": venue,
                "date": show_date,
                "ticket_url": ticket_url,
                "source_url": display_url,
            })
        
        # If the page returned fewer shows than per-page limit, it's the last page
        if len(page_shows) < SHOWS_PER_PAGE:
            print(f" -> Last page reached ({len(page_shows)} < {SHOWS_PER_PAGE} per page).")
            break
            
        page += 1

    return shows_data

if __name__ == "__main__":
    from enrich_venues import enrich_unenriched_venues
    from enrich_artists import enrich_unenriched_artists

    db_name = "concerts.db"
    conn = init_db(db_name)
    cursor = conn.cursor()
    
    print("="*50)
    print("Starting Oh My Rockness Concert ETL Scraper")
    print("="*50)
    
    scraped_shows = scrape_oh_my_rockness()
    
    total_scraped = len(scraped_shows)
    new_added = 0
    
    print("\n" + "="*50)
    print(f"Scraped {total_scraped} total shows. Processing database insertions...")
    print("="*50)
    
    for idx, show in enumerate(scraped_shows, 1):
        if not show['bands'] and show['venue'] == "Unknown Venue":
            print(f"[{idx}/{total_scraped}] Skipping invalid show (missing artists & venue).")
            continue

        venue_id = get_or_create_venue(cursor, show['venue'])

        inserted = insert_show(
            cursor,
            show['omr_show_id'],
            venue_id,
            show['date'],
            show['ticket_url'],
            show['source_url'],
        )

        band_names = ", ".join(b['name'] for b in show['bands']) or "Unknown Artist"
        status_msg = "NEWLY ADDED" if inserted > 0 else "DUPLICATE (SKIPPED)"
        print(f"[{idx}/{total_scraped}] {band_names} @ {show['venue']} ({show['date']}) -> {status_msg}")

        if inserted > 0:
            new_added += 1
            show_id = cursor.lastrowid
            for bill_order, band in enumerate(show['bands'], 1):
                artist_id = get_or_create_artist(
                    cursor, band['name'], band.get('id'), band.get('slug')
                )
                link_artist_to_show(cursor, show_id, artist_id, bill_order)
            
    # Commit all show insertions before enrichment
    conn.commit()
    
    print("\n" + "="*50)
    print("--- SCRAPE SUMMARY ---")
    print(f"Total shows scraped and parsed: {total_scraped}")
    print(f"Brand-new shows successfully added to DB: {new_added}")
    print(f"Skipped duplicate shows: {total_scraped - new_added}")
    print("="*50)
    
    # Enrich any venues that haven't been enriched yet
    print("\n" + "="*50)
    print("Starting venue enrichment via Perplexity Sonar API...")
    print("="*50)
    enrich_unenriched_venues(conn)

    # Enrich eligible artists (depends on venue capacity data, so runs after venues)
    print("\n" + "="*50)
    print("Starting artist enrichment via Perplexity Sonar API...")
    print("="*50)
    enrich_unenriched_artists(conn)

    conn.close()

    print("\n" + "="*50)
    print("--- ETL JOB COMPLETE ---")
    print("Database updated and connection closed.")
    print("="*50)
