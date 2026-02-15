#!/usr/bin/env python3
"""Tests for Snapchat Spotlight Tracker."""

import json
import math
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Set DB path before importing app
_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("SCRAPE_DELAY_MIN", "0")
os.environ.setdefault("SCRAPE_DELAY_MAX", "0")

import app as tracker

tracker.DB_PATH = Path(_tmpdir) / "test.db"
tracker.SCRAPE_DELAY_MIN = 0
tracker.SCRAPE_DELAY_MAX = 0


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    tracker.DB_PATH = tmp_path / "test.db"
    tracker.init_db()
    yield


@pytest.fixture
def client():
    tracker.app.config["TESTING"] = True
    with tracker.app.test_client() as c:
        yield c


# --- Outlier scoring ---

class TestOutlierScoring:
    def test_single_item_gets_score_1(self, client):
        db = tracker.get_db()
        db.execute("INSERT INTO spotlights (id, creator_username, view_count, share_count) VALUES ('a', 'user1', 1000, 10)")
        db.commit()
        tracker.recalculate_outlier_scores(db, 'user1')
        row = db.execute("SELECT outlier_score FROM spotlights WHERE id='a'").fetchone()
        assert row[0] == 1.0
        db.close()

    def test_outlier_gets_high_score(self, client):
        db = tracker.get_db()
        # Insert several normal + one outlier
        for i in range(5):
            db.execute("INSERT INTO spotlights (id, creator_username, view_count, share_count) VALUES (?, 'user1', ?, 0)",
                        (f'n{i}', 1000))
        db.execute("INSERT INTO spotlights (id, creator_username, view_count, share_count) VALUES ('outlier', 'user1', 100000, 0)")
        db.commit()
        tracker.recalculate_outlier_scores(db, 'user1')
        outlier = db.execute("SELECT outlier_score FROM spotlights WHERE id='outlier'").fetchone()
        normal = db.execute("SELECT outlier_score FROM spotlights WHERE id='n0'").fetchone()
        assert outlier[0] > normal[0]
        assert outlier[0] >= 7.0  # Should be high
        db.close()

    def test_scores_bounded_0_to_10(self, client):
        db = tracker.get_db()
        db.execute("INSERT INTO spotlights (id, creator_username, view_count, share_count) VALUES ('low', 'u', 0, 0)")
        db.execute("INSERT INTO spotlights (id, creator_username, view_count, share_count) VALUES ('high', 'u', 999999999, 0)")
        db.commit()
        tracker.recalculate_outlier_scores(db, 'u')
        rows = db.execute("SELECT outlier_score FROM spotlights").fetchall()
        for r in rows:
            assert 0 <= r[0] <= 10
        db.close()


# --- API endpoints ---

class TestAPI:
    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json["status"] == "ok"

    def test_dashboard(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"Snapchat Spotlight Tracker" in r.data

    def test_stats_empty(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200
        assert r.json["total_spotlights"] == 0

    def test_spotlights_empty(self, client):
        r = client.get("/api/spotlights")
        assert r.status_code == 200
        assert r.json == []

    def test_creators_empty(self, client):
        r = client.get("/api/creators")
        assert r.status_code == 200
        assert r.json == []

    def test_scrape_missing_username(self, client):
        r = client.post("/api/scrape", json={})
        assert r.status_code == 400

    @patch("app.scrape_profile")
    def test_scrape_success(self, mock_scrape, client):
        mock_scrape.return_value = {
            "userProfile": {"publicProfileInfo": {"title": "Test", "subscriberCount": "100"}},
            "spotlightHighlights": [{
                "storyId": {"value": "story1"},
                "snapList": [{"snapId": {"value": "snap1"}, "snapUrls": {"mediaUrl": "http://x"}}],
                "thumbnailUrl": {"value": "http://thumb"}
            }],
            "spotlightStoryMetadata": [{
                "videoMetadata": {"viewCount": "5000", "shareCount": "50", "durationMs": "15000"},
                "hashtags": ["funny"]
            }]
        }
        r = client.post("/api/scrape", json={"username": "testuser"})
        assert r.status_code == 200
        assert r.json["count"] == 1

        # Verify data stored
        r2 = client.get("/api/creators")
        assert len(r2.json) == 1
        assert r2.json[0]["username"] == "testuser"

    @patch("app.scrape_profile")
    def test_scrape_error_logged(self, mock_scrape, client):
        mock_scrape.side_effect = Exception("network error")
        r = client.post("/api/scrape", json={"username": "baduser"})
        assert r.status_code == 500


# --- DB operations ---

class TestDB:
    def test_init_creates_tables(self, client):
        db = tracker.get_db()
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {t[0] for t in tables}
        assert "creators" in names
        assert "spotlights" in names
        assert "scrape_log" in names
        assert "manual_entries" in names
        db.close()

    def test_creator_upsert(self, client):
        db = tracker.get_db()
        db.execute("INSERT INTO creators (username, display_name, last_scraped_at) VALUES ('u1', 'First', datetime('now'))")
        db.execute("INSERT INTO creators (username, display_name, last_scraped_at) VALUES ('u1', 'Updated', datetime('now')) ON CONFLICT(username) DO UPDATE SET display_name=excluded.display_name")
        db.commit()
        row = db.execute("SELECT display_name FROM creators WHERE username='u1'").fetchone()
        assert row[0] == "Updated"
        db.close()


# --- Manual entries ---

class TestManualEntry:
    def test_create_manual_entry(self, client):
        r = client.post("/api/manual", json={
            "url": "https://snapchat.com/spotlight/xxx",
            "creator_username": "someone",
            "estimated_views": 50000,
            "description": "Funny cat video"
        })
        assert r.status_code == 200
        assert "saved" in r.json["message"].lower()

        db = tracker.get_db()
        row = db.execute("SELECT * FROM manual_entries").fetchone()
        assert row is not None
        assert row["estimated_views"] == 50000
        db.close()


# --- Watchlist ---

class TestWatchlist:
    def test_load_watchlist(self, tmp_path):
        wl = tmp_path / "watchlist.txt"
        wl.write_text("# comment\nmrbeast\n\ncharlidamelio\n")
        tracker.WATCHLIST_PATH = wl
        names = tracker.load_watchlist()
        assert names == ["mrbeast", "charlidamelio"]

    def test_watchlist_api(self, client, tmp_path):
        wl = tmp_path / "watchlist.txt"
        wl.write_text("user1\nuser2\n")
        tracker.WATCHLIST_PATH = wl
        r = client.get("/api/watchlist")
        assert r.status_code == 200
        assert r.json["usernames"] == ["user1", "user2"]

    @patch("app.scrape_profile")
    def test_scrape_watchlist(self, mock_scrape, client, tmp_path):
        wl = tmp_path / "watchlist.txt"
        wl.write_text("testuser\n")
        tracker.WATCHLIST_PATH = wl
        mock_scrape.return_value = {
            "userProfile": {"publicProfileInfo": {"title": "T", "subscriberCount": "10"}},
            "spotlightHighlights": [],
            "spotlightStoryMetadata": []
        }
        r = client.post("/api/scrape-watchlist")
        assert r.status_code == 200
        assert r.json["summary"]["scraped"] == 1
