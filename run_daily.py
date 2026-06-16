"""
Daily orchestrator for the NYC Concert Tracker.

Intended to be run once per day via Windows Task Scheduler (3:00 AM EST).

Pipeline:
    1. Scrape Oh My Rockness for newly announced shows
    2. Enrich any new venues (only those not already in the DB)
    3. Enrich eligible new artists (based on venue tier + bill position)
    4. Generate a newsletter containing only shows announced since the last send
    5. Email the newsletter via Gmail SMTP
    6. Log success to the newsletter_log table

If no new shows were announced, the script exits silently (no email sent).
"""

import sqlite3
import os
import sys
import logging
from datetime import datetime, timezone

# Ensure imports work regardless of working directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
os.chdir(SCRIPT_DIR)

from scraper import init_db, scrape_oh_my_rockness, get_or_create_venue, get_or_create_artist, insert_show, link_artist_to_show
from enrich_venues import enrich_unenriched_venues
from enrich_artists import enrich_unenriched_artists
from enrich_links import enrich_links
from generate_newsletter import load_priority_venues, load_blacklisted_venues, load_shows, render_newsletter_markdown, convert_to_html, score_show, TIER_LABELS
from send_email import send_newsletter_email

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "daily_run.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Newsletter log table (tracks when newsletters were last sent)
# ---------------------------------------------------------------------------
def _init_newsletter_log(conn: sqlite3.Connection):
    """Create the newsletter_log table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS newsletter_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT NOT NULL,
            shows_included INTEGER,
            status TEXT DEFAULT 'success'
        )
    """)
    conn.commit()


def _get_last_sent_date(conn: sqlite3.Connection) -> str | None:
    """
    Get the date of the last successful newsletter send.

    Returns an ISO date string (e.g. '2026-06-07') or None if no
    newsletter has ever been sent (in which case we include all shows).
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sent_at FROM newsletter_log
        WHERE status = 'success'
        ORDER BY sent_at DESC
        LIMIT 1
    """)
    row = cursor.fetchone()
    if row:
        # sent_at is a full ISO timestamp; extract just the date portion
        return row[0][:10]
    return None


def _log_newsletter_sent(conn: sqlite3.Connection, shows_included: int):
    """Record a successful newsletter send."""
    timestamp = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO newsletter_log (sent_at, shows_included, status) VALUES (?, ?, 'success')",
        (timestamp, shows_included),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def run():
    log.info("=" * 50)
    log.info("NYC Concert Tracker — Daily Run Starting")
    log.info("=" * 50)

    # --- Step 0: Initialize DB ---
    db_path = os.path.join(SCRIPT_DIR, "concerts.db")
    conn = init_db(db_path)
    _init_newsletter_log(conn)
    cursor = conn.cursor()

    # --- Step 1: Scrape ---
    log.info("Step 1: Scraping Oh My Rockness...")
    scraped_shows = scrape_oh_my_rockness()
    log.info(f"Scraped {len(scraped_shows)} total show listings from OMR.")

    new_added = 0
    for show in scraped_shows:
        if not show["bands"] and show["venue"] == "Unknown Venue":
            continue

        venue_id = get_or_create_venue(cursor, show["venue"])
        inserted = insert_show(
            cursor,
            show["omr_show_id"],
            venue_id,
            show["date"],
            show["ticket_url"],
            show["source_url"],
        )

        if inserted > 0:
            new_added += 1
            show_id = cursor.lastrowid
            for bill_order, band in enumerate(show["bands"], 1):
                artist_id = get_or_create_artist(
                    cursor, band["name"], band.get("id"), band.get("slug")
                )
                link_artist_to_show(cursor, show_id, artist_id, bill_order)

    conn.commit()
    log.info(f"New shows added to DB: {new_added}")

    # --- Early exit if nothing new ---
    if new_added == 0:
        log.info("No new shows today. Exiting silently (no email).")
        conn.close()
        return

    # --- Step 2: Enrich new venues ---
    log.info("Step 2: Enriching any new venues...")
    enrich_unenriched_venues(conn)

    # --- Step 3: Enrich new artists ---
    log.info("Step 3: Enriching eligible new artists...")
    enrich_unenriched_artists(conn)

    # --- Step 3b: Enrich artist links (Spotify + YouTube) ---
    log.info("Step 3b: Enriching artist links (Spotify + YouTube)...")
    enrich_links(conn)

    # --- Step 4: Generate newsletter (only new shows) ---
    log.info("Step 4: Generating newsletter...")
    last_sent = _get_last_sent_date(conn)
    if last_sent:
        log.info(f"Last newsletter sent: {last_sent}. Including shows announced after that date.")
    else:
        log.info("No previous newsletter found. Including all shows.")

    priority_venues = load_priority_venues()
    blacklisted_venues = load_blacklisted_venues()
    shows = load_shows(conn, since_date=last_sent)

    # Filter out blacklisted venues
    shows = [s for s in shows if s["venue"].strip().lower() not in blacklisted_venues]

    if not shows:
        log.info("No shows to include in newsletter after date filtering. Exiting.")
        conn.close()
        return

    md_text = render_newsletter_markdown(shows, priority_venues)
    html_text = convert_to_html(md_text)

    # Save a copy to output/
    output_dir = os.path.join(SCRIPT_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    with open(os.path.join(output_dir, f"newsletter_{today_str}.html"), "w", encoding="utf-8") as f:
        f.write(html_text)

    # --- Step 5: Send email ---
    log.info(f"Step 5: Sending newsletter with {len(shows)} shows...")
    subject_date = datetime.now().strftime("%B %#d, %Y")
    subject = f"OMR NYC Show Announcements - {subject_date}"

    send_newsletter_email(subject=subject, html_body=html_text, plain_body=md_text)

    # --- Step 6: Log success ---
    _log_newsletter_sent(conn, shows_included=len(shows))
    log.info(f"Newsletter sent successfully. {len(shows)} shows included.")

    # Tier breakdown for the log
    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    for show in shows:
        tier = score_show(show, priority_venues)
        tier_counts[tier] += 1
    for tier in range(1, 5):
        log.info(f"  {TIER_LABELS[tier]}: {tier_counts[tier]}")

    conn.close()
    log.info("Daily run complete.")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.exception(f"Daily run failed with error: {e}")
        sys.exit(1)
