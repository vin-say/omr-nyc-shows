# NYC Concert Tracker

An automated pipeline that scrapes newly announced NYC concert listings from [Oh My Rockness](https://www.ohmyrockness.com), enriches them with venue metadata and artist research, scores them by relevance, and delivers a prioritized newsletter via email.

## Problem

Oh My Rockness publishes a rolling "Just Announced" feed of upcoming NYC shows, but the volume is high and there's no built-in way to filter for shows you'd actually care about. This project solves that by:

1. Automatically scraping new announcements daily
2. Enriching each show with venue capacity data and artist media coverage research
3. Scoring and ranking shows based on configurable venue preferences and whether artists have been covered by trusted music publications
4. Delivering a newsletter sorted by relevance so the most interesting shows appear first

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  run_daily.py  (orchestrator, triggered by Windows Task Scheduler)  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  1. scraper.py          → Scrape OMR JSON API, insert new shows     │
│  2. enrich_venues.py    → Perplexity Sonar Pro: venue metadata      │
│  3. enrich_artists.py   → Perplexity Sonar: artist reports          │
│  4. generate_newsletter.py → Score, tier, render Markdown + HTML    │
│  5. send_email.py       → Gmail SMTP delivery                       │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
OMR JSON API → scraper.py → concerts.db → enrich_venues.py  (Perplexity Sonar Pro)
                                        → enrich_artists.py (Perplexity Sonar)
                                        → generate_newsletter.py → HTML email
```

## Scoring Model

Shows are sorted into four tiers:

| Tier | Criteria | Description |
|------|----------|-------------|
| 1 | Priority venue **AND** media coverage | Top picks — interesting artists at favorite venues |
| 2 | Media coverage only | Artists covered by trusted publications, any venue |
| 3 | Priority venue only | Favorite venues, lesser-known artists |
| 4 | Neither | Everything else |

**Priority venues** are defined in `priority_venues.yaml` (editable without touching code).

**Media coverage** is detected from the structured JSON enrichment data — any artist with at least one `covered: true` entry is considered media-covered. A legacy fallback uses negative-phrase regex scanning for older free-form reports.

### Artist Enrichment Eligibility

Not every artist on every bill gets enriched. Eligibility depends on venue size and bill position:

| Venue Capacity Tier | Enriched Artists |
|---------------------|-----------------|
| Small / Small-Medium | Headliner only (bill_order = 1) |
| Medium | Top 2 on the bill (bill_order <= 2) |
| Large / Major | All artists |
| Unknown / NULL | All artists |

## Database Schema

### `venues`

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment ID |
| name | TEXT UNIQUE | Venue name (as listed on OMR) |
| address | TEXT | Full street address |
| is_primary_music_venue | INTEGER | 1 if dedicated music venue, 0 if secondary |
| venue_type_notes | TEXT | Brief description (e.g., "bar with live music stage") |
| capacity_tier | TEXT | One of: Small, Small-Medium, Medium, Large, Major, Unknown |
| enrichment_timestamp | TEXT | ISO timestamp of when venue was enriched |

### `artists`

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment ID |
| name | TEXT | Artist/band name |
| omr_id | INTEGER UNIQUE | Oh My Rockness internal band ID |
| omr_slug | TEXT | OMR URL slug (e.g., `death-from-above-1979`) |
| report | TEXT | Legacy free-form Markdown report (deprecated, kept for reference) |
| coverage_json | TEXT | Structured JSON: genres, publication coverage, and citation URLs |
| enrichment_timestamp | TEXT | ISO timestamp of when artist was enriched |

### `shows`

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment ID |
| omr_show_id | INTEGER UNIQUE | OMR internal show ID (used for deduplication) |
| venue_id | INTEGER FK | References venues.id |
| show_date | TEXT | Date of the show (YYYY-MM-DD) |
| announcement_date | DATE | When the show was first scraped (defaults to CURRENT_DATE) |
| ticket_url | TEXT | Link to purchase tickets (nullable) |
| source_url | TEXT | OMR page URL where the show was listed |

### `show_artists` (junction table)

| Column | Type | Description |
|--------|------|-------------|
| show_id | INTEGER FK | References shows.id |
| artist_id | INTEGER FK | References artists.id |
| bill_order | INTEGER | Position on the bill (1 = headliner, 2+ = openers) |

Composite primary key on (show_id, artist_id).

### `newsletter_log`

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment ID |
| sent_at | TEXT | ISO timestamp of successful send |
| shows_included | INTEGER | Number of shows in that newsletter issue |
| status | TEXT | 'success' (used for querying last successful send) |

## File Structure

```
NYC Concert Tracker/
├── scraper.py              # OMR JSON API scraper + DB schema init
├── enrich_venues.py        # Venue enrichment via Perplexity Sonar Pro
├── enrich_artists.py       # Artist enrichment via Perplexity Sonar
├── generate_newsletter.py  # Scoring, Markdown/HTML rendering
├── send_email.py           # Gmail SMTP email delivery
├── run_daily.py            # Daily orchestrator (calls all of the above)
├── run_daily.bat           # Windows Task Scheduler entry point
├── priority_venues.yaml    # Configurable list of priority venues
├── .env                    # API keys and email credentials (not in git)
├── .gitignore
├── README.md
├── concerts.db             # SQLite database (not in git, regenerated by scraper)
├── output/                 # Generated newsletter files (not in git)
│   └── newsletter_YYYY-MM-DD.html
├── logs/                   # Runtime logs (not in git)
│   └── daily_run.log
└── venv/                   # Python virtual environment (not in git)
```

## Setup

### Prerequisites

- Python 3.11+
- A [Perplexity API key](https://docs.perplexity.ai/) (for venue and artist enrichment)
- A Gmail account with [App Password](https://support.google.com/accounts/answer/185833) enabled

### Installation

```bash
git clone <repo-url>
cd "NYC Concert Tracker"
python -m venv venv
venv\Scripts\activate        # Windows
pip install curl-cffi python-dotenv pyyaml markdown
```

### Configuration

Create a `.env` file in the project root:

```
PERPLEXITY_API_KEY = pplx-your-key-here
GMAIL_SENDER = sender@gmail.com
GMAIL_APP_PASSWORD = sixteencharpassw
NEWSLETTER_RECIPIENT = recipient@gmail.com
```

Note: Gmail App Passwords are 16 characters with **no dashes or spaces**.

Edit `priority_venues.yaml` to set your preferred venues.

### Scheduling (Windows Task Scheduler)

1. Open Task Scheduler → Create Basic Task
2. **Name**: `NYC Concert Tracker Daily`
3. **Trigger**: Daily at 3:00 AM
4. **Action**: Start a program → browse to `run_daily.bat`
5. **Start in**: full path to the project folder
6. Under Properties → Settings: check "Run task as soon as possible after a scheduled start is missed"

## Usage

### Full daily pipeline (automated)

```bash
python run_daily.py
```

Scrapes, enriches, generates newsletter, sends email. Exits silently if no new shows.

### Individual components (manual)

```bash
# Scrape only (no enrichment or email)
python scraper.py

# Enrich unenriched venues
python enrich_venues.py

# Enrich eligible unenriched artists
python enrich_artists.py

# Generate newsletter (all shows)
python generate_newsletter.py

# Generate newsletter (only shows announced after a date)
python generate_newsletter.py --since 2026-06-01
```

## Methodology

### Scraping

The scraper uses Oh My Rockness's internal JSON API (`/api/shows.json`) rather than HTML parsing. This API is the same endpoint their frontend lazy-loads from — it provides structured data including individual band objects with unique IDs, venue info, dates, and ticket URLs. Authentication uses a public token embedded in OMR's JavaScript bundle.

The `curl_cffi` library is used with Chrome impersonation to bypass Cloudflare protection. Requests are rate-limited to one page every 3 seconds.

### Enrichment

**Venues** are enriched via Perplexity Sonar Pro with structured JSON output. The prompt asks for address, whether the venue is primarily a music venue, a description of the venue type, and a capacity tier classification.

**Artists** are enriched via Perplexity Sonar (standard) with high search context and structured JSON output. The JSON schema enforces a consistent format with two fields: `genres` (array of specific genre labels) and `coverage` (array of objects with `publication`, `covered` boolean, `context` description, and `citation_index`). The Perplexity citations array is embedded in the stored JSON so the newsletter renderer can produce inline hyperlinks. Only publications where `covered=true` are shown in the newsletter — "no evidence" entries are silently omitted.

### Newsletter Generation

The newsletter generator:
1. Loads priority venues from YAML
2. Queries shows (optionally filtered by announcement date)
3. Detects media coverage using a negative-phrase regex filter on artist reports
4. Scores each show into tiers 1-4
5. Renders Markdown sorted by tier, then by show date
6. Converts to HTML with inline CSS (email-safe)

### Deduplication

- **Shows**: deduplicated by `omr_show_id` (OMR's internal show identifier)
- **Artists**: deduplicated by `omr_id` first, then by name
- **Venues**: deduplicated by exact name match
- **Newsletter**: only shows announced after the last successful newsletter send are included

## Dependencies

| Package | Purpose |
|---------|---------|
| `curl-cffi` | HTTP requests with Chrome impersonation (Cloudflare bypass) |
| `python-dotenv` | Load `.env` configuration |
| `pyyaml` | Parse priority venues config |
| `markdown` | Convert Markdown newsletter to HTML |
