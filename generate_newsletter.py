"""
Newsletter generator for NYC Concert Tracker.

Reads show data from concerts.db, scores each show into one of four tiers
based on venue priority and artist media coverage, then renders a Markdown
newsletter and converts it to an HTML email.

Tiers (highest to lowest):
  1. Priority venue AND at least one artist with media coverage
  2. At least one artist with media coverage (non-priority venue)
  3. Priority venue (no artist media coverage)
  4. Everything else

Usage:
    python generate_newsletter.py              # writes output/ files
    python generate_newsletter.py --db path    # custom DB path
"""

import sqlite3
import os
import re
import json
import argparse
from datetime import datetime
from urllib.parse import quote

import yaml
import markdown

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(SCRIPT_DIR, "concerts.db")
PRIORITY_VENUES_FILE = os.path.join(SCRIPT_DIR, "priority_venues.yaml")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

PUBLICATIONS = [
    "Pitchfork",
    "Brooklyn Vegan",
    "Bandcamp Daily",
    "Resident Advisor",
    "KEXP",
]

# Phrases that indicate NO coverage when found on the same line as a
# publication name.  Used as a fallback for legacy free-form reports.
_NEGATIVE_PHRASES = [
    "no evidence",
    "not find",
    "no coverage",
    "not appear",
    "no specific",
    "not featured",
    "could not",
    "no direct",
    "no mention",
    "not mentioned",
    "no results",
    "not covered",
    "couldn't find",
    "couldn't verify",
    "no confirmed",
    "cannot confirm",
    "no kexp coverage",
]

TIER_LABELS = {
    1: "Priority Venue + Media Coverage",
    2: "Media Coverage",
    3: "Priority Venue",
    4: "Other Shows",
}


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------
def load_priority_venues(path: str = PRIORITY_VENUES_FILE) -> set[str]:
    """Load the priority venue list from the YAML config file."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    venues = data.get("priority_venues", [])
    return {v.strip().lower() for v in venues}


def load_blacklisted_venues(path: str = PRIORITY_VENUES_FILE) -> set[str]:
    """Load the blacklisted venue list from the YAML config file."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    venues = data.get("blacklisted_venues", [])
    return {v.strip().lower() for v in venues}


# ---------------------------------------------------------------------------
# Coverage detection
# ---------------------------------------------------------------------------
def _has_media_coverage_json(coverage_json: str | None) -> bool:
    """Check structured JSON for at least one covered=true publication."""
    if not coverage_json:
        return False
    try:
        data = json.loads(coverage_json)
        return any(c.get("covered") for c in data.get("coverage", []))
    except (json.JSONDecodeError, TypeError):
        return False


def _has_media_coverage_legacy(report: str | None) -> bool:
    """
    Fallback: scan a free-form Markdown report for coverage mentions.

    For each publication, find the line that mentions it.
    If the line does NOT contain any of the known negative phrases,
    the artist is considered to have coverage for that outlet.
    """
    if not report:
        return False

    lines = report.split("\n")
    for pub in PUBLICATIONS:
        pub_lower = pub.lower()
        for line in lines:
            if pub_lower in line.lower():
                line_lower = line.lower()
                is_negative = any(neg in line_lower for neg in _NEGATIVE_PHRASES)
                if not is_negative:
                    return True
                break  # move to next publication
    return False


def _has_media_coverage(coverage_json: str | None, report: str | None) -> bool:
    """Determine if an artist has media coverage using the best available data."""
    if coverage_json:
        return _has_media_coverage_json(coverage_json)
    return _has_media_coverage_legacy(report)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_shows(conn: sqlite3.Connection, since_date: str | None = None) -> list[dict]:
    """
    Load shows with their venue, artists, reports, and ticket info.

    Args:
        conn: SQLite connection.
        since_date: If provided (ISO date string like '2026-06-01'), only
                    returns shows with announcement_date > since_date.
                    If None, returns all shows.

    Returns a list of show dicts, each with nested artist list.
    """
    cursor = conn.cursor()

    # Fetch shows with venue info, optionally filtered by announcement date
    if since_date:
        cursor.execute("""
            SELECT s.id, s.show_date, s.ticket_url, s.source_url,
                   v.name AS venue_name
            FROM shows s
            JOIN venues v ON v.id = s.venue_id
            WHERE s.announcement_date > ?
            ORDER BY s.show_date
        """, (since_date,))
    else:
        cursor.execute("""
            SELECT s.id, s.show_date, s.ticket_url, s.source_url,
                   v.name AS venue_name
            FROM shows s
            JOIN venues v ON v.id = s.venue_id
            ORDER BY s.show_date
        """)
    shows_raw = cursor.fetchall()

    shows = []
    for show_id, show_date, ticket_url, source_url, venue_name in shows_raw:
        # Fetch artists for this show, ordered by bill position
        cursor.execute("""
            SELECT a.name, a.report, a.coverage_json, sa.bill_order,
                   a.spotify_url, a.youtube_url
            FROM show_artists sa
            JOIN artists a ON a.id = sa.artist_id
            WHERE sa.show_id = ?
            ORDER BY sa.bill_order
        """, (show_id,))
        artists = [
            {
                "name": name,
                "report": report,
                "coverage_json": coverage_json,
                "bill_order": bill_order,
                "spotify_url": spotify_url,
                "youtube_url": youtube_url,
            }
            for name, report, coverage_json, bill_order, spotify_url, youtube_url
            in cursor.fetchall()
        ]

        shows.append({
            "id": show_id,
            "show_date": show_date,
            "ticket_url": ticket_url,
            "source_url": source_url,
            "venue": venue_name,
            "artists": artists,
        })

    return shows


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_show(show: dict, priority_venues: set[str]) -> int:
    """
    Assign a tier (1-4) to a show.

    1 = priority venue AND media coverage
    2 = media coverage only
    3 = priority venue only
    4 = neither
    """
    is_priority = show["venue"].strip().lower() in priority_venues
    has_coverage = any(
        _has_media_coverage(a.get("coverage_json"), a.get("report"))
        for a in show["artists"]
    )

    if is_priority and has_coverage:
        return 1
    elif has_coverage:
        return 2
    elif is_priority:
        return 3
    else:
        return 4


# ---------------------------------------------------------------------------
# Artist report rendering (from structured JSON)
# ---------------------------------------------------------------------------
def _render_artist_from_json(coverage_json: str) -> str | None:
    """
    Render an artist's structured JSON enrichment data into clean Markdown.

    Only includes publications where covered=true, with inline hyperlinks.
    Returns None if there's nothing meaningful to show.
    """
    try:
        data = json.loads(coverage_json)
    except (json.JSONDecodeError, TypeError):
        return None

    genres = data.get("genres", [])
    coverage = data.get("coverage", [])
    citations = data.get("citations", [])

    # Filter out placeholder/meaningless genre entries
    genres = [g for g in genres if g.lower() not in ("unknown", "n/a", "none", "")]

    lines = []

    # Genres line
    if genres:
        lines.append(f"**Genres:** {', '.join(genres)}")

    # Coverage — only show publications that DID cover the artist
    covered_items = [c for c in coverage if c.get("covered")]
    if covered_items:
        lines.append("")
        lines.append("**Coverage:**")
        for item in covered_items:
            pub = item["publication"]
            context = item.get("context") or ""
            citation_idx = item.get("citation_index")

            # Build inline hyperlink from citation index
            if citation_idx and 1 <= citation_idx <= len(citations):
                url = citations[citation_idx - 1]
                if context:
                    lines.append(f"- **{pub}**: {context} ([source]({url}))")
                else:
                    lines.append(f"- **{pub}**: [source]({url})")
            else:
                if context:
                    lines.append(f"- **{pub}**: {context}")
                else:
                    lines.append(f"- **{pub}**")

    if not lines:
        return None

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------
def _format_date(date_str: str) -> str:
    """Convert YYYY-MM-DD to a human-readable date with day name."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%A, %B %-d, %Y")
    except (ValueError, AttributeError):
        # %-d is not supported on Windows; fall back to %#d
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return dt.strftime("%A, %B %#d, %Y")
        except Exception:
            return date_str


def _build_gcal_url(show: dict) -> str:
    """Build a Google Calendar 'Add Event' URL for a show."""
    lineup = ", ".join(a["name"] for a in show["artists"])
    title = f"[OMR] {lineup} @ {show['venue']}"
    # All-day event: dates are YYYYMMDD/YYYYMMDD (end is exclusive, so +1 day)
    try:
        dt = datetime.strptime(show["show_date"], "%Y-%m-%d")
        from datetime import timedelta
        start = dt.strftime("%Y%m%d")
        end = (dt + timedelta(days=1)).strftime("%Y%m%d")
    except ValueError:
        start = show["show_date"].replace("-", "")
        end = start
    dates = f"{start}/{end}"
    params = (
        f"action=TEMPLATE"
        f"&text={quote(title)}"
        f"&dates={dates}"
        f"&location={quote(show['venue'])}"
    )
    if show.get("ticket_url"):
        params += f"&details={quote(show['ticket_url'])}"
    return f"https://calendar.google.com/calendar/render?{params}"


def _render_show_markdown(show: dict) -> str:
    """Render a single show as a Markdown section."""
    lines = []

    # Headline: lineup @ venue
    lineup = ", ".join(a["name"] for a in show["artists"])
    lines.append(f"### {lineup}")
    lines.append(f"**{show['venue']}** — {_format_date(show['show_date'])}")
    lines.append("")

    # Ticket + calendar links
    show_links = []
    if show["ticket_url"]:
        show_links.append(f"[Tickets]({show['ticket_url']})")
    show_links.append(f"[Add to Calendar]({_build_gcal_url(show)})")
    lines.append(" | ".join(show_links))
    lines.append("")

    # Artist reports — prefer structured JSON, fall back to legacy Markdown
    artists_with_info = []
    for artist in show["artists"]:
        rendered = None
        if artist.get("coverage_json"):
            rendered = _render_artist_from_json(artist["coverage_json"])
        elif artist.get("report"):
            rendered = artist["report"].strip()

        # Build listen links line
        links = []
        if artist.get("spotify_url"):
            links.append(f"[Spotify]({artist['spotify_url']})")
        if artist.get("youtube_url"):
            links.append(f"[Live Video]({artist['youtube_url']})")

        has_content = rendered or links
        if has_content:
            artists_with_info.append({
                "name": artist["name"],
                "rendered": rendered,
                "is_json": bool(artist.get("coverage_json")),
                "links": links,
            })

    for info in artists_with_info:
        if len(artists_with_info) > 1:
            lines.append(f"**{info['name']}:**")
            lines.append("")

        if info["rendered"]:
            for line in info["rendered"].split("\n"):
                lines.append(f"> {line}")

        if info["links"]:
            links_line = " | ".join(info["links"])
            if info["rendered"]:
                lines.append(f"> ")
                lines.append(f"> {links_line}")
            else:
                lines.append(f"> {links_line}")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def render_newsletter_markdown(
    shows: list[dict], priority_venues: set[str]
) -> str:
    """
    Score all shows, sort them by tier then date, and render the full
    newsletter as Markdown.
    """
    # Score and group
    scored = []
    for show in shows:
        tier = score_show(show, priority_venues)
        scored.append((tier, show))

    # Sort by tier (ascending = highest priority first), then by date
    scored.sort(key=lambda x: (x[0], x[1]["show_date"]))

    # Build the document
    today = datetime.now().strftime("%B %#d, %Y")
    parts = [
        f"# NYC Concert Tracker — New Announcements",
        f"*Generated {today}*",
        "",
    ]

    # Tier summary counts
    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    for tier, _ in scored:
        tier_counts[tier] += 1

    parts.append(
        f"**{len(shows)} shows** — "
        f"{tier_counts[1]} top picks, "
        f"{tier_counts[2]} media-covered, "
        f"{tier_counts[3]} priority venue, "
        f"{tier_counts[4]} other"
    )
    parts.append("")

    current_tier = None
    for tier, show in scored:
        if tier != current_tier:
            current_tier = tier
            parts.append(f"## {TIER_LABELS[tier]}")
            parts.append("")
        parts.append(_render_show_markdown(show))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# HTML conversion
# ---------------------------------------------------------------------------
HTML_WRAPPER = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NYC Concert Tracker</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 Helvetica, Arial, sans-serif;
    max-width: 700px;
    margin: 2rem auto;
    padding: 0 1rem;
    color: #1a1a1a;
    line-height: 1.6;
  }}
  h1 {{
    border-bottom: 3px solid #1a1a1a;
    padding-bottom: 0.4rem;
  }}
  h2 {{
    color: #b33;
    margin-top: 2.5rem;
    border-bottom: 1px solid #ddd;
    padding-bottom: 0.3rem;
  }}
  h3 {{
    margin-bottom: 0.2rem;
  }}
  blockquote {{
    border-left: 3px solid #ccc;
    margin: 0.5rem 0 1rem 0;
    padding: 0.4rem 0.8rem;
    color: #444;
    background: #fafafa;
    font-size: 0.92em;
  }}
  blockquote p {{
    margin: 0.3rem 0;
  }}
  blockquote ul {{
    margin: 0.3rem 0;
    padding-left: 1.2rem;
  }}
  a {{
    color: #b33;
  }}
  hr {{
    border: none;
    border-top: 1px solid #eee;
    margin: 1.5rem 0;
  }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def convert_to_html(md_text: str) -> str:
    """Convert Markdown newsletter to a self-contained HTML email."""
    body = markdown.markdown(md_text, extensions=["extra"])
    return HTML_WRAPPER.format(body=body)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Generate concert newsletter")
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to concerts.db")
    parser.add_argument(
        "--since",
        default=None,
        help="Only include shows announced after this date (ISO format, e.g. 2026-06-01)",
    )
    args = parser.parse_args()

    # Load data
    priority_venues = load_priority_venues()
    blacklisted_venues = load_blacklisted_venues()
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON;")
    shows = load_shows(conn, since_date=args.since)
    conn.close()

    # Filter out blacklisted venues
    shows = [s for s in shows if s["venue"].strip().lower() not in blacklisted_venues]

    print(f"Loaded {len(shows)} shows ({len(blacklisted_venues)} blacklisted venues filtered).")

    # Generate
    md_text = render_newsletter_markdown(shows, priority_venues)
    html_text = convert_to_html(md_text)

    # Write output
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    md_path = os.path.join(OUTPUT_DIR, "newsletter.md")
    html_path = os.path.join(OUTPUT_DIR, "newsletter.html")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_text)

    print(f"Newsletter written to:\n  Markdown: {md_path}\n  HTML:     {html_path}")

    # Print tier breakdown
    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    for show in shows:
        tier = score_show(show, priority_venues)
        tier_counts[tier] += 1

    print("\nTier breakdown:")
    for tier in range(1, 5):
        print(f"  {TIER_LABELS[tier]}: {tier_counts[tier]}")


if __name__ == "__main__":
    main()
