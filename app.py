#!/usr/bin/env python3
"""Snapchat Spotlight Viral Content Tracker.

Scrapes public Snapchat profiles via web (__NEXT_DATA__ JSON),
extracts Spotlight content with engagement metrics,
stores in SQLite, and calculates outlier scores.
"""

import sqlite3
import json
import os
import re
import time
import math
import random
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, jsonify, request, render_template_string

DB_PATH = Path("/data/snapchat_tracker.db")
WATCHLIST_PATH = Path("/app/watchlist.txt")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Rate limit config from env
SCRAPE_DELAY_MIN = int(os.environ.get("SCRAPE_DELAY_MIN", 3))
SCRAPE_DELAY_MAX = int(os.environ.get("SCRAPE_DELAY_MAX", 8))
BACKOFF_BASE = 2
MAX_RETRIES = 3

app = Flask(__name__)


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS creators (
            username TEXT PRIMARY KEY,
            display_name TEXT,
            subscriber_count INTEGER DEFAULT 0,
            profile_picture_url TEXT,
            last_scraped_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS spotlights (
            id TEXT PRIMARY KEY,
            creator_username TEXT,
            snap_id TEXT,
            media_url TEXT,
            thumbnail_url TEXT,
            view_count INTEGER DEFAULT 0,
            share_count INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            width INTEGER,
            height INTEGER,
            caption TEXT,
            hashtags TEXT,
            upload_date_ms TEXT,
            uploaded_at TEXT,
            first_seen_at TEXT DEFAULT (datetime('now')),
            last_updated_at TEXT DEFAULT (datetime('now')),
            outlier_score REAL DEFAULT 0,
            FOREIGN KEY (creator_username) REFERENCES creators(username)
        );
        CREATE TABLE IF NOT EXISTS scrape_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            status TEXT,
            spotlights_found INTEGER DEFAULT 0,
            error_message TEXT,
            scraped_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS manual_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            creator_username TEXT,
            description TEXT,
            estimated_views INTEGER,
            estimated_shares INTEGER,
            category TEXT,
            notes TEXT,
            outlier_score REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_spotlights_views ON spotlights(view_count DESC);
        CREATE INDEX IF NOT EXISTS idx_spotlights_score ON spotlights(outlier_score DESC);
        CREATE INDEX IF NOT EXISTS idx_spotlights_creator ON spotlights(creator_username);
    """)
    db.commit()
    db.close()


def load_watchlist() -> list[str]:
    """Load usernames from watchlist.txt."""
    if not WATCHLIST_PATH.exists():
        return []
    names = []
    for line in WATCHLIST_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line.replace("@", ""))
    return names


def scrape_profile(username: str) -> dict:
    """Scrape a Snapchat public profile page for spotlight data."""
    url = f"https://www.snapchat.com/@{username}"
    headers = {"User-Agent": USER_AGENT}

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            break
        except requests.RequestException:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = BACKOFF_BASE ** (attempt + 1) + random.uniform(0, 1)
            time.sleep(wait)

    match = re.search(r'__NEXT_DATA__.*?type="application/json">(.*?)</script>', resp.text)
    if not match:
        raise ValueError("Could not find __NEXT_DATA__ in page")

    data = json.loads(match.group(1))
    props = data.get("props", {}).get("pageProps", {})
    return props


def extract_and_store(username: str, props: dict) -> int:
    """Extract spotlight data from page props and store in DB."""
    db = get_db()

    profile = props.get("userProfile", {}).get("publicProfileInfo", {})
    db.execute("""
        INSERT INTO creators (username, display_name, subscriber_count, profile_picture_url, last_scraped_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(username) DO UPDATE SET
            display_name = excluded.display_name,
            subscriber_count = excluded.subscriber_count,
            profile_picture_url = excluded.profile_picture_url,
            last_scraped_at = datetime('now')
    """, (
        username,
        profile.get("title", username),
        int(profile.get("subscriberCount", "0")),
        profile.get("profilePictureUrl", ""),
    ))

    highlights = props.get("spotlightHighlights", [])
    metadata_list = props.get("spotlightStoryMetadata", [])
    count = 0

    for i, highlight in enumerate(highlights):
        story_id = highlight.get("storyId", {}).get("value", "")
        if not story_id:
            continue

        snap_id = ""
        media_url = ""
        snaps = highlight.get("snapList", [])
        if snaps:
            snap = snaps[0]
            snap_id = snap.get("snapId", {}).get("value", "")
            urls = snap.get("snapUrls", {})
            media_url = urls.get("mediaUrl", "")

        meta = metadata_list[i] if i < len(metadata_list) else {}
        video_meta = meta.get("videoMetadata", {})
        hashtags = [h for h in meta.get("hashtags", [])]

        view_count = int(video_meta.get("viewCount", "0"))
        share_count = int(video_meta.get("shareCount", "0"))
        duration_ms = int(video_meta.get("durationMs", "0"))
        width = video_meta.get("width", 0)
        height = video_meta.get("height", 0)
        caption = video_meta.get("embeddedTextCaption", "")
        upload_date_ms = video_meta.get("uploadDateMs", "")

        uploaded_at = ""
        if upload_date_ms:
            try:
                uploaded_at = datetime.fromtimestamp(
                    int(upload_date_ms) / 1000, tz=timezone.utc
                ).isoformat()
            except (ValueError, OSError):
                pass

        content_id = hashlib.md5(f"{username}:{story_id}".encode()).hexdigest()

        db.execute("""
            INSERT INTO spotlights (id, creator_username, snap_id, media_url, thumbnail_url,
                view_count, share_count, duration_ms, width, height, caption, hashtags,
                upload_date_ms, uploaded_at, last_updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                view_count = MAX(excluded.view_count, spotlights.view_count),
                share_count = MAX(excluded.share_count, spotlights.share_count),
                last_updated_at = datetime('now')
        """, (
            content_id, username, snap_id, media_url,
            highlight.get("thumbnailUrl", {}).get("value", ""),
            view_count, share_count, duration_ms, width, height,
            caption, json.dumps(hashtags), upload_date_ms, uploaded_at,
        ))
        count += 1

    db.commit()
    recalculate_outlier_scores(db, username)

    db.execute("""
        INSERT INTO scrape_log (username, status, spotlights_found)
        VALUES (?, 'success', ?)
    """, (username, count))
    db.commit()
    db.close()
    return count


def recalculate_outlier_scores(db, username: Optional[str] = None):
    """Calculate outlier scores using z-score method."""
    if username:
        rows = db.execute(
            "SELECT id, view_count, share_count FROM spotlights WHERE creator_username = ?",
            (username,)
        ).fetchall()
    else:
        rows = db.execute("SELECT id, view_count, share_count FROM spotlights").fetchall()

    if len(rows) < 2:
        for r in rows:
            db.execute("UPDATE spotlights SET outlier_score = 1.0 WHERE id = ?", (r["id"],))
        db.commit()
        return

    views = [r["view_count"] for r in rows]
    mean_v = sum(views) / len(views)
    std_v = math.sqrt(sum((v - mean_v) ** 2 for v in views) / len(views)) or 1

    for r in rows:
        z_view = (r["view_count"] - mean_v) / std_v
        score = min(10, max(0, 5 + z_view * 2))
        db.execute("UPDATE spotlights SET outlier_score = ? WHERE id = ?", (round(score, 2), r["id"]))
    db.commit()


# === Flask Routes ===

DASHBOARD_HTML = """<!DOCTYPE html>
<html><head><title>Snapchat Spotlight Tracker</title>
<style>
body{font-family:-apple-system,sans-serif;max-width:1200px;margin:0 auto;padding:20px;background:#1a1a2e;color:#e0e0e0}
h1{color:#fffc00}h2{color:#fffc00;margin-top:30px}
table{width:100%;border-collapse:collapse;margin:10px 0}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #333}
th{background:#16213e;color:#fffc00}tr:hover{background:#16213e}
.score{font-weight:bold;padding:2px 8px;border-radius:4px}
.score-high{background:#ff4444;color:white}.score-mid{background:#ffaa00;color:black}.score-low{background:#44aa44;color:white}
input,button,select{padding:8px 12px;margin:4px;border:1px solid #333;background:#16213e;color:#e0e0e0;border-radius:4px}
button{background:#fffc00;color:#1a1a2e;cursor:pointer;font-weight:bold}button:hover{background:#e6e300}
.card{background:#16213e;padding:15px;border-radius:8px;margin:10px 0}
.stats{display:flex;gap:20px;flex-wrap:wrap}
.stat{background:#16213e;padding:15px 20px;border-radius:8px;text-align:center}
.stat-val{font-size:24px;font-weight:bold;color:#fffc00}.stat-label{font-size:12px;color:#888}
.thumb{width:60px;height:80px;object-fit:cover;border-radius:4px}
a{color:#fffc00}
.watchlist-status{background:#16213e;padding:15px;border-radius:8px;margin:10px 0}
.wl-user{display:inline-block;background:#1a1a2e;padding:4px 10px;border-radius:12px;margin:3px;font-size:13px}
.last-scrape{color:#888;font-size:13px;margin-top:8px}
</style></head><body>
<h1>👻 Snapchat Spotlight Tracker</h1>
<div class="stats" id="stats"></div>
<div class="last-scrape" id="lastScrape"></div>

<div class="watchlist-status">
<h2 style="margin-top:0">📋 Watchlist</h2>
<div id="watchlistUsers"></div>
<button onclick="scrapeWatchlist()" id="wlBtn">🔄 Scrape All Watchlist</button>
<span id="wlResult" style="margin-left:10px"></span>
</div>

<div class="card">
<h2 style="margin-top:0">Track Creator</h2>
<form id="scrapeForm" style="display:flex;align-items:center;gap:8px">
<input id="username" placeholder="username (e.g. mrbeast)" style="flex:1">
<button type="submit">🔍 Scrape</button>
</form>
<div id="scrapeResult" style="margin-top:8px"></div>
</div>
<h2>🔥 Top Spotlights by Outlier Score</h2>
<table id="spotlightsTable"><thead><tr>
<th>Thumb</th><th>Creator</th><th>Views</th><th>Shares</th><th>Duration</th><th>Uploaded</th><th>Score</th>
</tr></thead><tbody></tbody></table>
<h2>👤 Tracked Creators</h2>
<table id="creatorsTable"><thead><tr>
<th>Username</th><th>Display Name</th><th>Subscribers</th><th>Last Scraped</th><th>Action</th>
</tr></thead><tbody></tbody></table>
<h2>📝 Manual Entry</h2>
<div class="card">
<form id="manualForm">
<input id="m_url" placeholder="Snapchat URL" style="width:100%;margin-bottom:8px"><br>
<input id="m_creator" placeholder="Creator username">
<input id="m_views" placeholder="Est. views" type="number">
<input id="m_shares" placeholder="Est. shares" type="number"><br>
<input id="m_desc" placeholder="Description" style="width:100%;margin:8px 0"><br>
<input id="m_notes" placeholder="Notes" style="width:100%;margin-bottom:8px"><br>
<button type="submit">💾 Save Manual Entry</button>
</form>
</div>
<script>
async function loadData(){
  const [spots,creators,stats,wl]=await Promise.all([
    fetch('/api/spotlights?limit=50').then(r=>r.json()),
    fetch('/api/creators').then(r=>r.json()),
    fetch('/api/stats').then(r=>r.json()),
    fetch('/api/watchlist').then(r=>r.json())
  ]);
  document.getElementById('stats').innerHTML=`
    <div class="stat"><div class="stat-val">${stats.total_spotlights}</div><div class="stat-label">Spotlights</div></div>
    <div class="stat"><div class="stat-val">${stats.total_creators}</div><div class="stat-label">Creators</div></div>
    <div class="stat"><div class="stat-val">${fmt(stats.total_views)}</div><div class="stat-label">Total Views</div></div>
    <div class="stat"><div class="stat-val">${stats.avg_outlier_score?.toFixed(1)||'—'}</div><div class="stat-label">Avg Score</div></div>`;
  if(stats.last_scrape_at){
    document.getElementById('lastScrape').textContent='Last scrape: '+new Date(stats.last_scrape_at+'Z').toLocaleString();
  }
  document.getElementById('watchlistUsers').innerHTML=wl.usernames.map(u=>`<span class="wl-user">@${u}</span>`).join('');
  const stb=document.querySelector('#spotlightsTable tbody');
  stb.innerHTML=spots.map(s=>`<tr>
    <td>${s.thumbnail_url?`<img class="thumb" src="${s.thumbnail_url}">`:'—'}</td>
    <td><a href="https://snapchat.com/@${s.creator_username}" target="_blank">${s.creator_username}</a></td>
    <td>${fmt(s.view_count)}</td><td>${fmt(s.share_count)}</td>
    <td>${(s.duration_ms/1000).toFixed(0)}s</td>
    <td>${s.uploaded_at?new Date(s.uploaded_at).toLocaleDateString():'—'}</td>
    <td><span class="score ${s.outlier_score>=7?'score-high':s.outlier_score>=4?'score-mid':'score-low'}">${s.outlier_score?.toFixed(1)}</span></td>
  </tr>`).join('');
  const ctb=document.querySelector('#creatorsTable tbody');
  ctb.innerHTML=creators.map(c=>`<tr>
    <td><a href="https://snapchat.com/@${c.username}" target="_blank">@${c.username}</a></td>
    <td>${c.display_name||''}</td><td>${fmt(c.subscriber_count)}</td>
    <td>${c.last_scraped_at||'never'}</td>
    <td><button onclick="scrape('${c.username}')">🔄</button></td>
  </tr>`).join('');
}
function fmt(n){if(!n)return'0';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'K';return n.toString()}
async function scrape(u){
  document.getElementById('scrapeResult').textContent='Scraping...';
  const r=await fetch('/api/scrape',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u})});
  const d=await r.json();
  document.getElementById('scrapeResult').textContent=d.message||d.error;
  loadData();
}
async function scrapeWatchlist(){
  document.getElementById('wlBtn').disabled=true;
  document.getElementById('wlResult').textContent='Scraping watchlist...';
  const r=await fetch('/api/scrape-watchlist',{method:'POST'});
  const d=await r.json();
  document.getElementById('wlResult').textContent=JSON.stringify(d.summary||d);
  document.getElementById('wlBtn').disabled=false;
  loadData();
}
document.getElementById('scrapeForm').onsubmit=e=>{e.preventDefault();scrape(document.getElementById('username').value.trim().replace('@',''))};
document.getElementById('manualForm').onsubmit=async e=>{
  e.preventDefault();
  const r=await fetch('/api/manual',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    url:document.getElementById('m_url').value,creator_username:document.getElementById('m_creator').value,
    estimated_views:parseInt(document.getElementById('m_views').value)||0,
    estimated_shares:parseInt(document.getElementById('m_shares').value)||0,
    description:document.getElementById('m_desc').value,notes:document.getElementById('m_notes').value
  })});
  const d=await r.json();alert(d.message||d.error);e.target.reset();
};
loadData();setInterval(loadData,60000);
</script></body></html>"""


@app.route("/")
def dashboard():
    return DASHBOARD_HTML


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    data = request.json or {}
    username = data.get("username", "").strip().replace("@", "")
    if not username:
        return jsonify({"error": "username required"}), 400
    try:
        props = scrape_profile(username)
        count = extract_and_store(username, props)
        return jsonify({"message": f"Scraped {count} spotlights from @{username}", "count": count})
    except Exception as e:
        db = get_db()
        db.execute("INSERT INTO scrape_log (username, status, error_message) VALUES (?, 'error', ?)",
                    (username, str(e)))
        db.commit()
        db.close()
        return jsonify({"error": str(e)}), 500


@app.route("/api/scrape-watchlist", methods=["POST"])
def api_scrape_watchlist():
    """Scrape all creators from watchlist.txt with rate limiting."""
    usernames = load_watchlist()
    if not usernames:
        return jsonify({"error": "Watchlist is empty or not found"}), 404

    results = {}
    for i, username in enumerate(usernames):
        try:
            props = scrape_profile(username)
            count = extract_and_store(username, props)
            results[username] = {"status": "ok", "count": count}
        except Exception as e:
            db = get_db()
            db.execute("INSERT INTO scrape_log (username, status, error_message) VALUES (?, 'error', ?)",
                        (username, str(e)))
            db.commit()
            db.close()
            results[username] = {"status": "error", "error": str(e)}

        # Rate limit between scrapes (not after last one)
        if i < len(usernames) - 1:
            delay = random.uniform(SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX)
            time.sleep(delay)

    ok = sum(1 for r in results.values() if r["status"] == "ok")
    total_spots = sum(r.get("count", 0) for r in results.values() if r["status"] == "ok")
    return jsonify({
        "results": results,
        "summary": {"scraped": ok, "errors": len(results) - ok, "total_spotlights": total_spots}
    })


@app.route("/api/scrape-all", methods=["POST"])
def api_scrape_all():
    """Scrape all tracked creators in DB."""
    db = get_db()
    creators = db.execute("SELECT username FROM creators").fetchall()
    db.close()
    results = {}
    for i, c in enumerate(creators):
        try:
            props = scrape_profile(c["username"])
            count = extract_and_store(c["username"], props)
            results[c["username"]] = {"status": "ok", "count": count}
        except Exception as e:
            results[c["username"]] = {"status": "error", "error": str(e)}
        if i < len(creators) - 1:
            delay = random.uniform(SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX)
            time.sleep(delay)
    return jsonify(results)


@app.route("/api/watchlist")
def api_watchlist():
    """Return current watchlist."""
    return jsonify({"usernames": load_watchlist()})


@app.route("/api/spotlights")
def api_spotlights():
    limit = request.args.get("limit", 50, type=int)
    creator = request.args.get("creator", "")
    db = get_db()
    if creator:
        rows = db.execute(
            "SELECT * FROM spotlights WHERE creator_username = ? ORDER BY outlier_score DESC LIMIT ?",
            (creator, limit)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM spotlights ORDER BY outlier_score DESC LIMIT ?", (limit,)).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/creators")
def api_creators():
    db = get_db()
    rows = db.execute("SELECT * FROM creators ORDER BY subscriber_count DESC").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/stats")
def api_stats():
    db = get_db()
    stats = {}
    stats["total_spotlights"] = db.execute("SELECT COUNT(*) FROM spotlights").fetchone()[0]
    stats["total_creators"] = db.execute("SELECT COUNT(*) FROM creators").fetchone()[0]
    stats["total_views"] = db.execute("SELECT COALESCE(SUM(view_count),0) FROM spotlights").fetchone()[0]
    stats["avg_outlier_score"] = db.execute("SELECT AVG(outlier_score) FROM spotlights").fetchone()[0]
    row = db.execute("SELECT MAX(scraped_at) as last FROM scrape_log WHERE status='success'").fetchone()
    stats["last_scrape_at"] = row["last"] if row else None
    db.close()
    return jsonify(stats)


@app.route("/api/manual", methods=["POST"])
def api_manual():
    data = request.json or {}
    db = get_db()
    db.execute("""
        INSERT INTO manual_entries (url, creator_username, description, estimated_views, estimated_shares, notes)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (data.get("url", ""), data.get("creator_username", ""), data.get("description", ""),
          data.get("estimated_views", 0), data.get("estimated_shares", 0), data.get("notes", "")))
    db.commit()
    db.close()
    return jsonify({"message": "Manual entry saved"})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    init_db()
    app.run(host="0.0.0.0", port=5000)
