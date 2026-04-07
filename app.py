import hashlib
import json
import os
import random
import traceback
from datetime import datetime, date, timedelta

import requests
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-change-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///analytics.db"
)
db = SQLAlchemy(app)

# --- Configuration ---
# Set these in your environment or .env file
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_CHANNEL_HANDLE = os.environ.get("YOUTUBE_HANDLE", "@parkerprintss")
TIKTOK_USERNAME = os.environ.get("TIKTOK_USERNAME", "parkerprintss")
INSTAGRAM_USERNAME = os.environ.get("INSTAGRAM_USERNAME", "parkerprintss")

# Cache duration in minutes - avoid hitting APIs too often
CACHE_MINUTES = 15


# --- Models ---


class PlatformSnapshot(db.Model):
    """Stores a snapshot of platform analytics at a point in time."""

    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(20), nullable=False)  # youtube, tiktok, instagram
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)
    data = db.Column(db.Text, nullable=False)  # JSON blob of all metrics


class Settings(db.Model):
    """Stores user settings like API keys."""

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(500), nullable=False)


# --- Data Fetching ---


def get_setting(key, default=""):
    s = Settings.query.filter_by(key=key).first()
    return s.value if s else default


def set_setting(key, value):
    s = Settings.query.filter_by(key=key).first()
    if s:
        s.value = value
    else:
        s = Settings(key=key, value=value)
        db.session.add(s)
    db.session.commit()


def get_cached_data(platform):
    """Get the most recent snapshot if it's fresh enough."""
    cutoff = datetime.utcnow() - timedelta(minutes=CACHE_MINUTES)
    snapshot = (
        PlatformSnapshot.query.filter_by(platform=platform)
        .filter(PlatformSnapshot.fetched_at > cutoff)
        .order_by(PlatformSnapshot.fetched_at.desc())
        .first()
    )
    if snapshot:
        return json.loads(snapshot.data)
    return None


def save_snapshot(platform, data):
    snapshot = PlatformSnapshot(
        platform=platform,
        data=json.dumps(data),
    )
    db.session.add(snapshot)
    db.session.commit()
    return data


def get_all_snapshots(platform, limit=30):
    """Get historical snapshots for a platform."""
    snapshots = (
        PlatformSnapshot.query.filter_by(platform=platform)
        .order_by(PlatformSnapshot.fetched_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {"fetched_at": s.fetched_at, "data": json.loads(s.data)} for s in snapshots
    ]


# --- YouTube Fetcher ---


def fetch_youtube_data():
    """Fetch YouTube channel stats using the Data API v3."""
    cached = get_cached_data("youtube")
    if cached:
        return cached

    api_key = get_setting("youtube_api_key", YOUTUBE_API_KEY)
    handle = get_setting("youtube_handle", YOUTUBE_CHANNEL_HANDLE)

    if not api_key:
        return {
            "error": "YouTube API key not set. Go to Settings to add it.",
            "subscribers": 0,
            "total_views": 0,
            "video_count": 0,
            "channel_name": handle,
            "recent_videos": [],
        }

    try:
        # First, find the channel by handle
        search_url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "part": "snippet",
            "q": handle,
            "type": "channel",
            "key": api_key,
            "maxResults": 1,
        }
        resp = requests.get(search_url, params=params, timeout=10)
        resp.raise_for_status()
        results = resp.json()

        if not results.get("items"):
            return {"error": f"Channel {handle} not found", "subscribers": 0,
                    "total_views": 0, "video_count": 0, "channel_name": handle,
                    "recent_videos": []}

        channel_id = results["items"][0]["snippet"]["channelId"]
        channel_name = results["items"][0]["snippet"]["title"]

        # Get channel statistics
        ch_url = "https://www.googleapis.com/youtube/v3/channels"
        params = {
            "part": "statistics,snippet",
            "id": channel_id,
            "key": api_key,
        }
        resp = requests.get(ch_url, params=params, timeout=10)
        resp.raise_for_status()
        ch_data = resp.json()

        stats = ch_data["items"][0]["statistics"]

        # Get recent videos
        vid_url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "part": "snippet",
            "channelId": channel_id,
            "order": "date",
            "type": "video",
            "maxResults": 10,
            "key": api_key,
        }
        resp = requests.get(vid_url, params=params, timeout=10)
        resp.raise_for_status()
        vid_results = resp.json()

        video_ids = [v["id"]["videoId"] for v in vid_results.get("items", [])]
        videos = []

        if video_ids:
            # Get video statistics
            vs_url = "https://www.googleapis.com/youtube/v3/videos"
            params = {
                "part": "statistics,snippet,contentDetails",
                "id": ",".join(video_ids),
                "key": api_key,
            }
            resp = requests.get(vs_url, params=params, timeout=10)
            resp.raise_for_status()
            vs_data = resp.json()

            for v in vs_data.get("items", []):
                videos.append({
                    "title": v["snippet"]["title"],
                    "published": v["snippet"]["publishedAt"][:10],
                    "views": int(v["statistics"].get("viewCount", 0)),
                    "likes": int(v["statistics"].get("likeCount", 0)),
                    "comments": int(v["statistics"].get("commentCount", 0)),
                    "thumbnail": v["snippet"]["thumbnails"].get("medium", {}).get("url", ""),
                })

        data = {
            "channel_name": channel_name,
            "channel_id": channel_id,
            "subscribers": int(stats.get("subscriberCount", 0)),
            "total_views": int(stats.get("viewCount", 0)),
            "video_count": int(stats.get("videoCount", 0)),
            "recent_videos": videos,
            "error": None,
        }
        return save_snapshot("youtube", data)

    except Exception as e:
        return {
            "error": f"Failed to fetch YouTube data: {str(e)}",
            "subscribers": 0,
            "total_views": 0,
            "video_count": 0,
            "channel_name": handle,
            "recent_videos": [],
        }


# --- TikTok Fetcher ---


def fetch_tiktok_data():
    """Fetch TikTok public profile data."""
    cached = get_cached_data("tiktok")
    if cached:
        return cached

    username = get_setting("tiktok_username", TIKTOK_USERNAME)

    try:
        # Use TikTok's public oembed endpoint and web scraping
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            ),
        }

        # Try the oembed API for basic profile info
        url = f"https://www.tiktok.com/oembed?url=https://www.tiktok.com/@{username}"
        resp = requests.get(url, headers=headers, timeout=10)

        # TikTok public data is limited without official API
        # We'll store what we can get and allow manual override
        data = {
            "username": username,
            "followers": 0,
            "following": 0,
            "likes": 0,
            "videos": 0,
            "recent_videos": [],
            "error": None,
            "note": "TikTok limits public data access. Stats update when available.",
        }

        # Try to get some data from the profile page
        profile_url = f"https://www.tiktok.com/@{username}"
        resp = requests.get(profile_url, headers=headers, timeout=10)

        if resp.status_code == 200:
            text = resp.text
            # Try to extract JSON data from the page
            import re

            # Look for the SIGI_STATE or similar data blob
            patterns = [
                r'"followerCount"\s*:\s*(\d+)',
                r'"followingCount"\s*:\s*(\d+)',
                r'"heartCount"\s*:\s*(\d+)',
                r'"videoCount"\s*:\s*(\d+)',
            ]
            keys = ["followers", "following", "likes", "videos"]
            for pattern, key in zip(patterns, keys):
                match = re.search(pattern, text)
                if match:
                    data[key] = int(match.group(1))

            if data["followers"] > 0:
                data["note"] = None

        return save_snapshot("tiktok", data)

    except Exception as e:
        return {
            "username": username,
            "followers": 0,
            "following": 0,
            "likes": 0,
            "videos": 0,
            "recent_videos": [],
            "error": f"Failed to fetch TikTok data: {str(e)}",
            "note": None,
        }


# --- Instagram Fetcher ---


def fetch_instagram_data():
    """Fetch Instagram public profile data."""
    cached = get_cached_data("instagram")
    if cached:
        return cached

    username = get_setting("instagram_username", INSTAGRAM_USERNAME)

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            ),
        }

        data = {
            "username": username,
            "followers": 0,
            "following": 0,
            "posts": 0,
            "full_name": username,
            "bio": "",
            "profile_pic": "",
            "recent_posts": [],
            "error": None,
            "note": None,
        }

        # Try Instagram's public JSON endpoint
        url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
        headers_ig = {
            **headers,
            "X-IG-App-ID": "936619743392459",
        }
        resp = requests.get(url, headers=headers_ig, timeout=10)

        if resp.status_code == 200:
            try:
                profile = resp.json()["data"]["user"]
                data["followers"] = profile.get("edge_followed_by", {}).get("count", 0)
                data["following"] = profile.get("edge_follow", {}).get("count", 0)
                data["posts"] = profile.get("edge_owner_to_timeline_media", {}).get(
                    "count", 0
                )
                data["full_name"] = profile.get("full_name", username)
                data["bio"] = profile.get("biography", "")
                data["profile_pic"] = profile.get("profile_pic_url_hd", "")

                # Get recent posts
                edges = (
                    profile.get("edge_owner_to_timeline_media", {})
                    .get("edges", [])[:10]
                )
                for edge in edges:
                    node = edge["node"]
                    data["recent_posts"].append({
                        "caption": (
                            node.get("edge_media_to_caption", {})
                            .get("edges", [{}])[0]
                            .get("node", {})
                            .get("text", "")[:100]
                            if node.get("edge_media_to_caption", {}).get("edges")
                            else ""
                        ),
                        "likes": node.get("edge_liked_by", {}).get("count", 0),
                        "comments": node.get("edge_media_to_comment", {}).get(
                            "count", 0
                        ),
                        "timestamp": datetime.fromtimestamp(
                            node.get("taken_at_timestamp", 0)
                        ).strftime("%b %d, %Y"),
                        "thumbnail": node.get("thumbnail_src", ""),
                        "is_video": node.get("is_video", False),
                    })
            except (KeyError, IndexError):
                data["note"] = "Instagram limits public data access. Some stats may be unavailable."
        else:
            data["note"] = "Instagram limits public data access. Some stats may be unavailable."

        return save_snapshot("instagram", data)

    except Exception as e:
        return {
            "username": username,
            "followers": 0,
            "following": 0,
            "posts": 0,
            "full_name": username,
            "bio": "",
            "profile_pic": "",
            "recent_posts": [],
            "error": f"Failed to fetch Instagram data: {str(e)}",
            "note": None,
        }


# --- Routes ---


@app.route("/")
def dashboard():
    yt = fetch_youtube_data()
    tt = fetch_tiktok_data()
    ig = fetch_instagram_data()
    today_ideas = get_daily_ideas(date.today())
    # Only show non-skipped ideas
    today_ideas = [i for i in today_ideas if i.status != "skipped"]
    return render_template("dashboard.html", yt=yt, tt=tt, ig=ig, ideas=today_ideas, today=date.today())


@app.route("/youtube")
def youtube():
    data = fetch_youtube_data()
    history = get_all_snapshots("youtube")
    return render_template("youtube.html", data=data, history=history)


@app.route("/tiktok")
def tiktok():
    data = fetch_tiktok_data()
    history = get_all_snapshots("tiktok")
    return render_template("tiktok.html", data=data, history=history)


@app.route("/instagram")
def instagram():
    data = fetch_instagram_data()
    history = get_all_snapshots("instagram")
    return render_template("instagram.html", data=data, history=history)


@app.route("/refresh/<platform>")
def refresh(platform):
    """Force refresh data for a platform by clearing cache."""
    if platform in ("youtube", "tiktok", "instagram"):
        # Delete recent cached data to force a refresh
        cutoff = datetime.utcnow() - timedelta(seconds=1)
        PlatformSnapshot.query.filter_by(platform=platform).filter(
            PlatformSnapshot.fetched_at > (datetime.utcnow() - timedelta(minutes=CACHE_MINUTES))
        ).delete()
        db.session.commit()

    return redirect(url_for(platform) if platform in ("youtube", "tiktok", "instagram") else url_for("dashboard"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        set_setting("youtube_api_key", request.form.get("youtube_api_key", ""))
        set_setting("youtube_handle", request.form.get("youtube_handle", "@parkerprintss"))
        set_setting("tiktok_username", request.form.get("tiktok_username", "parkerprintss"))
        set_setting("instagram_username", request.form.get("instagram_username", "parkerprintss"))
        flash("Settings saved!", "success")
        return redirect(url_for("settings"))

    return render_template(
        "settings.html",
        youtube_api_key=get_setting("youtube_api_key", YOUTUBE_API_KEY),
        youtube_handle=get_setting("youtube_handle", YOUTUBE_CHANNEL_HANDLE),
        tiktok_username=get_setting("tiktok_username", TIKTOK_USERNAME),
        instagram_username=get_setting("instagram_username", INSTAGRAM_USERNAME),
    )


# --- Video Ideas ---

# Content idea pools - organized by category
# Every 6th slot is reserved for a BUSINESS idea (non-Claude, non-AI)
CONTENT_IDEAS = {
    "creator": [
        {"title": "How I Film & Edit on My Phone in Under 30 Min", "platform": "all", "hook": "Most creators overcomplicate this..."},
        {"title": "The Exact Posting Schedule That Grew My Audience", "platform": "all", "hook": "I tested 5 different schedules and here's what worked"},
        {"title": "How to Repurpose 1 Video Into 10 Pieces of Content", "platform": "all", "hook": "Work smarter, not harder"},
        {"title": "Behind the Scenes: My Full Content Creation Setup", "platform": "youtube", "hook": "Everything I use to create content daily"},
        {"title": "5 Free Apps Every Content Creator Needs", "platform": "all", "hook": "These changed my workflow completely"},
        {"title": "How I Come Up With Content Ideas Every Single Day", "platform": "all", "hook": "Never run out of ideas again"},
        {"title": "Reacting to My First Ever Video", "platform": "youtube", "hook": "This is embarrassing but let's go"},
        {"title": "Day in the Life of a Full-Time Creator", "platform": "all", "hook": "What my average day actually looks like"},
        {"title": "The Truth About Being a Content Creator No One Talks About", "platform": "youtube", "hook": "It's not all what it seems"},
        {"title": "How to Start Creating Content With Zero Followers", "platform": "all", "hook": "I wish someone told me this when I started"},
    ],
    "growth": [
        {"title": "How I Gained My First 1,000 Followers", "platform": "all", "hook": "The strategy that actually worked"},
        {"title": "Why Your Videos Aren't Getting Views (And How to Fix It)", "platform": "youtube", "hook": "3 mistakes killing your reach"},
        {"title": "The Hook Formula That Gets People to Stop Scrolling", "platform": "tiktok", "hook": "Use this in your first 3 seconds"},
        {"title": "How the Algorithm Actually Works in 2026", "platform": "all", "hook": "Stop guessing, start understanding"},
        {"title": "Small Creator Tips That Big Creators Won't Tell You", "platform": "all", "hook": "These are game changers"},
        {"title": "I Tried Posting 3x a Day for 30 Days - Here's What Happened", "platform": "tiktok", "hook": "The results surprised me"},
        {"title": "How to Make Your Thumbnails Stand Out", "platform": "youtube", "hook": "Simple design tricks that boost CTR"},
        {"title": "Stop Chasing Trends - Do This Instead", "platform": "all", "hook": "Trends fade, this doesn't"},
    ],
    "lifestyle": [
        {"title": "My Morning Routine as a Creator", "platform": "all", "hook": "Productive but realistic"},
        {"title": "What I Spend in a Week as a Content Creator", "platform": "youtube", "hook": "Full transparency, no cap"},
        {"title": "Organizing My Life With These Simple Systems", "platform": "all", "hook": "From chaos to clarity"},
        {"title": "Things I Stopped Doing That Changed My Life", "platform": "all", "hook": "Sometimes less is more"},
        {"title": "My Top 5 Productivity Hacks That Actually Work", "platform": "all", "hook": "No fluff, just results"},
        {"title": "How I Stay Motivated When I Don't Feel Like Creating", "platform": "all", "hook": "Real talk about burnout"},
        {"title": "Room/Studio Tour & Setup Breakdown", "platform": "youtube", "hook": "Where the magic happens"},
        {"title": "Monthly Reset Routine - How I Plan My Content", "platform": "all", "hook": "Start each month with a clear plan"},
    ],
    "tech": [
        {"title": "Best Budget Tech for Content Creators in 2026", "platform": "youtube", "hook": "You don't need expensive gear"},
        {"title": "iPhone vs Android for Content Creation - Honest Take", "platform": "all", "hook": "Which one actually wins?"},
        {"title": "Apps on My Phone You've Never Heard Of", "platform": "tiktok", "hook": "Hidden gems that save me hours"},
        {"title": "My Favorite Shortcuts & Automations on My Phone", "platform": "all", "hook": "Work faster with these tricks"},
        {"title": "Gadgets I Regret Buying vs Gadgets Worth Every Dollar", "platform": "youtube", "hook": "Honest reviews only"},
        {"title": "How to Get Better Audio on Your Videos (No Expensive Mic)", "platform": "all", "hook": "Audio > video quality, always"},
    ],
    # BUSINESS ideas (non-Claude, non-AI) - every 6th video
    "business": [
        {"title": "How to Build Multiple Income Streams as a Creator", "platform": "youtube", "hook": "Don't rely on one source of income"},
        {"title": "The Business Side of Content Creation Nobody Teaches You", "platform": "youtube", "hook": "Taxes, contracts, and real talk"},
        {"title": "How to Price Yourself for Brand Deals", "platform": "all", "hook": "Stop undercharging - here's the formula"},
        {"title": "Building a Personal Brand That Lasts", "platform": "all", "hook": "Trends die, brands don't"},
        {"title": "How to Turn Your Content Into a Real Business", "platform": "youtube", "hook": "From hobby to LLC"},
        {"title": "Investing Basics Every Young Creator Should Know", "platform": "youtube", "hook": "Start building wealth now"},
        {"title": "How I Negotiate Brand Deals (With Real Examples)", "platform": "youtube", "hook": "The exact emails I send"},
        {"title": "Creating Products vs Services - Which Is Better for Creators?", "platform": "all", "hook": "Pros and cons of each"},
        {"title": "Financial Mistakes I Made as a Creator (So You Don't Have To)", "platform": "youtube", "hook": "Learn from my L's"},
        {"title": "How to Build an Email List That Actually Makes Money", "platform": "all", "hook": "Social media is rented land"},
        {"title": "Side Hustles That Actually Work in 2026", "platform": "all", "hook": "Tried and tested, not just theory"},
        {"title": "How to Read a Contract Before Signing a Brand Deal", "platform": "youtube", "hook": "Protect yourself - here's what to look for"},
    ],
}


class VideoIdea(db.Model):
    """Stores video ideas with status tracking."""

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    title = db.Column(db.String(300), nullable=False)
    platform = db.Column(db.String(20), default="all")
    hook = db.Column(db.String(500), default="")
    category = db.Column(db.String(50), default="creator")
    position = db.Column(db.Integer, default=0)  # 1-based position in the day's list
    status = db.Column(db.String(20), default="suggested")  # suggested, skipped, saved, filmed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def get_daily_ideas(target_date=None, force_refresh=False):
    """Generate or retrieve video ideas for a given date."""
    if target_date is None:
        target_date = date.today()

    # Check if we already have ideas for this date
    existing = VideoIdea.query.filter_by(date=target_date).order_by(VideoIdea.position).all()
    if existing and not force_refresh:
        return existing

    # If force refresh, delete old suggested ones (keep saved/filmed)
    if force_refresh:
        VideoIdea.query.filter_by(date=target_date, status="suggested").delete()
        VideoIdea.query.filter_by(date=target_date, status="skipped").delete()
        db.session.commit()
        # Keep saved/filmed ones
        kept = VideoIdea.query.filter_by(date=target_date).all()
        kept_titles = {v.title for v in kept}
    else:
        kept_titles = set()

    # Use date as seed for consistent but different ideas each day
    seed = int(hashlib.md5(target_date.isoformat().encode()).hexdigest(), 16)
    rng = random.Random(seed)

    # If refreshing, use a different seed
    if force_refresh:
        refresh_count = VideoIdea.query.filter_by(date=target_date).count()
        seed = int(hashlib.md5(f"{target_date.isoformat()}-{refresh_count}".encode()).hexdigest(), 16)
        rng = random.Random(seed)

    # Build the day's lineup: 5 content ideas + position 6 is always business
    categories = ["creator", "growth", "lifestyle", "tech"]
    ideas_to_add = []

    # Pick 5 ideas from non-business categories
    all_content = []
    for cat in categories:
        for idea in CONTENT_IDEAS[cat]:
            if idea["title"] not in kept_titles:
                all_content.append({**idea, "category": cat})

    rng.shuffle(all_content)
    selected = all_content[:5]

    # Pick 1 business idea for position 6
    biz_ideas = [i for i in CONTENT_IDEAS["business"] if i["title"] not in kept_titles]
    rng.shuffle(biz_ideas)
    biz_pick = biz_ideas[0] if biz_ideas else None

    # Create the ideas
    for i, idea in enumerate(selected, 1):
        vi = VideoIdea(
            date=target_date,
            title=idea["title"],
            platform=idea.get("platform", "all"),
            hook=idea.get("hook", ""),
            category=idea["category"],
            position=i,
            status="suggested",
        )
        db.session.add(vi)
        ideas_to_add.append(vi)

    if biz_pick:
        vi = VideoIdea(
            date=target_date,
            title=biz_pick["title"],
            platform=biz_pick.get("platform", "all"),
            hook=biz_pick.get("hook", ""),
            category="business",
            position=6,
            status="suggested",
        )
        db.session.add(vi)
        ideas_to_add.append(vi)

    db.session.commit()

    return VideoIdea.query.filter_by(date=target_date).order_by(VideoIdea.position).all()


@app.route("/ideas")
def ideas():
    today = date.today()
    daily = get_daily_ideas(today)
    saved = VideoIdea.query.filter_by(status="saved").order_by(VideoIdea.date.desc()).all()
    filmed = VideoIdea.query.filter_by(status="filmed").order_by(VideoIdea.date.desc()).limit(20).all()
    return render_template("ideas.html", ideas=daily, saved=saved, filmed=filmed, today=today)


@app.route("/ideas/refresh")
def ideas_refresh():
    get_daily_ideas(date.today(), force_refresh=True)
    flash("Fresh ideas loaded!", "success")
    return redirect(url_for("ideas"))


@app.route("/ideas/skip/<int:idea_id>")
def idea_skip(idea_id):
    idea = VideoIdea.query.get_or_404(idea_id)
    idea.status = "skipped"
    db.session.commit()

    # Generate a replacement idea in the same position
    target_date = idea.date
    seed = int(hashlib.md5(f"{target_date.isoformat()}-skip-{idea_id}".encode()).hexdigest(), 16)
    rng = random.Random(seed)

    existing_titles = {v.title for v in VideoIdea.query.filter_by(date=target_date).all()}

    if idea.category == "business":
        pool = [i for i in CONTENT_IDEAS["business"] if i["title"] not in existing_titles]
    else:
        pool = []
        for cat in ["creator", "growth", "lifestyle", "tech"]:
            pool.extend({**i, "category": cat} for i in CONTENT_IDEAS[cat] if i["title"] not in existing_titles)

    if pool:
        rng.shuffle(pool)
        pick = pool[0]
        replacement = VideoIdea(
            date=target_date,
            title=pick["title"],
            platform=pick.get("platform", "all"),
            hook=pick.get("hook", ""),
            category=pick.get("category", idea.category),
            position=idea.position,
            status="suggested",
        )
        db.session.add(replacement)
        db.session.commit()

    return redirect(url_for("ideas"))


@app.route("/ideas/save/<int:idea_id>")
def idea_save(idea_id):
    idea = VideoIdea.query.get_or_404(idea_id)
    idea.status = "saved"
    db.session.commit()
    flash(f"Saved: {idea.title}", "success")
    return redirect(url_for("ideas"))


@app.route("/ideas/filmed/<int:idea_id>")
def idea_filmed(idea_id):
    idea = VideoIdea.query.get_or_404(idea_id)
    idea.status = "filmed"
    db.session.commit()
    flash(f"Marked as filmed: {idea.title}", "success")
    return redirect(url_for("ideas"))


@app.route("/ideas/unsave/<int:idea_id>")
def idea_unsave(idea_id):
    idea = VideoIdea.query.get_or_404(idea_id)
    idea.status = "suggested"
    db.session.commit()
    return redirect(url_for("ideas"))


# --- Init DB ---

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
