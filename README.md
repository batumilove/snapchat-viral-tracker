# Snapchat Spotlight Viral Tracker

Web-based tracker for Snapchat Spotlight content. Scrapes public profiles, stores data in SQLite, calculates outlier scores using z-score method, and provides a web dashboard.

## Architecture

```
Snapchat Web (__NEXT_DATA__) → app.py (Flask) → SQLite → Z-score outlier scoring → Web dashboard
```

## Prerequisites

- Python 3.10+
- Docker & Docker Compose (for containerized usage)

## Quick Start (Local)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python3 app.py
# Open http://localhost:5000
```

## Docker Compose

```bash
docker compose build
docker compose up -d
# Open http://localhost:5007
```

## Features

- **Web Dashboard** — Real-time stats, top spotlights, creator tracking
- **Watchlist** — Bulk scrape from `watchlist.txt`
- **Manual Entries** — Add content manually via the dashboard
- **Outlier Scoring** — Z-score based scoring (0–10 scale)

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Dashboard |
| POST | `/api/scrape` | Scrape a creator `{"username": "..."}` |
| POST | `/api/scrape-watchlist` | Scrape all watchlist creators |
| GET | `/api/spotlights?limit=50` | Top spotlights |
| GET | `/api/creators` | Tracked creators |
| GET | `/api/stats` | Database statistics |
| POST | `/api/manual` | Add manual entry |
| GET | `/health` | Health check |

## Rate Limiting

- Configurable delay between scrapes (env: `SCRAPE_DELAY_MIN`, `SCRAPE_DELAY_MAX`)
- Exponential backoff with jitter on failures (max 3 retries)

## Tests

```bash
pytest test_app.py -v
```

## ⚠️ Disclaimer

Use responsibly and in compliance with Snapchat's Terms of Service.

## License

MIT
