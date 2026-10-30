"""
KickCuttr Engine - Video Clipping & Trimming for Kick.com
Supports:
- Kick VODs, Clips, and Livestream segment extraction.
- High-speed slicing via yt-dlp and ffmpeg.
- Quality selection (1080p, 720p, 480p, Best).
"""

import os
import re
import json
import datetime
import urllib.request
import subprocess
from pathlib import Path
from typing import Dict, Any, Tuple

KICK_API_CHANNEL = "https://kick.com/api/v2/channels/{slug}"
KICK_API_VIDEO = "https://kick.com/api/v1/video/{video_id}"

# -------------------------------------------------------------
# TIME HELPERS
# -------------------------------------------------------------
def time_to_seconds(t_str: str) -> int:
    """Converts strings like '01:30', '1:02:45', or '90' into integer seconds."""
    t_str = str(t_str).strip()
    if not t_str:
        return 0
    if t_str.isdigit():
        return int(t_str)
    
    parts = t_str.split(":")
    try:
        parts = [int(p) for p in parts]
        if len(parts) == 1:
            return parts[0]
        elif len(parts) == 2:
            return parts[0] * 60 + parts[1]
        elif len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except Exception:
        pass
    return 0


def seconds_to_time(seconds: int) -> str:
    """Converts integer seconds into HH:MM:SS format."""
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# -------------------------------------------------------------
# KICK VIDEO & CHANNEL INFO EXTRACTOR
# -------------------------------------------------------------
def extract_slug_from_url(url: str) -> str:
    """Extracts username slug or channel name from Kick URL."""
    url = url.strip()
    m = re.search(r'kick\.com/([a-zA-Z0-9_\-]+)', url)
    if m:
        slug = m.group(1).lower()
        if slug not in ['video', 'categories', 'search', 'following', 'clips']:
            return slug
    return ""


def get_kick_channel_data(slug: str) -> Dict[str, Any]:
    """Fetches public channel metadata from Kick v2 API."""
    slug = slug.strip().lower()
    if not slug:
        return {}
    url = KICK_API_CHANNEL.format(slug=slug)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"[Engine] Channel fetch warning for '{slug}': {e}")
        return {}


def get_kick_info(url_or_channel: str) -> Dict[str, Any]:
    """
    Extracts complete info (Title, Uploader, Duration, Stream ID, Thumbnail)
    using yt-dlp and Kick public APIs.
    """
    target = url_or_channel.strip()
    if not target.startswith("http"):
        slug = target.replace("@", "")
        target = f"https://kick.com/{slug}"
    else:
        slug = extract_slug_from_url(target)

    # 1. Fetch channel API data if slug exists (fast fallback)
    channel_api_data = {}
    if slug:
        channel_api_data = get_kick_channel_data(slug)

    livestream = channel_api_data.get("livestream") or {}
    stream_id = livestream.get("id") or channel_api_data.get("id") or "N/A"

    # 2. Query yt-dlp for video metadata
    cmd = ["yt-dlp", "--dump-json", "--no-warnings", target]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            info = json.loads(result.stdout.strip().splitlines()[0])
            duration = info.get("duration") or 0
            return {
                "success": True,
                "title": info.get("title") or info.get("fulltitle") or "Kick Stream/Video",
                "uploader": info.get("uploader") or info.get("channel") or slug or "Kick Creator",
                "stream_id": str(info.get("display_id") or stream_id),
                "duration": duration,
                "duration_str": seconds_to_time(duration) if duration else "Live / VOD",
                "thumbnail": info.get("thumbnail") or (livestream.get("thumbnail", {}).get("url") if isinstance(livestream.get("thumbnail"), dict) else livestream.get("thumbnail")),
                "webpage_url": info.get("webpage_url") or target,
                "is_live": info.get("is_live", False)
            }
    except Exception as e:
        print(f"[Engine] yt-dlp metadata error: {e}")

    # Fallback to channel API data if yt-dlp was slow
    if channel_api_data:
        return {
            "success": True,
            "title": livestream.get("session_title") or f"{slug.upper()}'s Kick Stream",
            "uploader": channel_api_data.get("user", {}).get("username") or slug,
            "stream_id": str(stream_id),
            "duration": livestream.get("duration") or 0,
            "duration_str": "Live Stream" if livestream.get("is_live") else "Recorded VOD",
            "thumbnail": livestream.get("thumbnail", {}).get("url") if isinstance(livestream.get("thumbnail"), dict) else None,
            "webpage_url": target,
            "is_live": livestream.get("is_live", False)
        }

    return {
        "success": False,
        "error": "Could not fetch Kick video or channel information. Please verify the URL."
    }


# -------------------------------------------------------------
# VIDEO CLIPPER (FFMPEG & YT-DLP)
# -------------------------------------------------------------
def clip_kick_video(
    url: str,
    start_time: str,
    end_time: str,
    quality: str = "720p",
    output_dir: str = "/tmp/kickcuttr"
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Clips a Kick VOD or stream clip between start_time and end_time.
    Uses yt-dlp with --download-sections and ffmpeg for fast download.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    start_sec = time_to_seconds(start_time)
    end_sec = time_to_seconds(end_time)

    if end_sec <= start_sec:
        return False, "End time must be greater than Start time.", {}

    clip_duration = end_sec - start_sec
    if clip_duration > 7200:  # 2 hours limit
        return False, "Clip duration cannot exceed 2 hours at a time.", {}

    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_filename = f"kick_clip_{timestamp_str}.mp4"
    final_output = out_path / out_filename

    # Format quality selector for yt-dlp
    format_selector = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
    if "1080" in quality:
        format_selector = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
    elif "480" in quality:
        format_selector = "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
    elif "best" in quality.lower():
        format_selector = "bestvideo+bestaudio/best"

    section_arg = f"*{seconds_to_time(start_sec)}-{seconds_to_time(end_sec)}"

    cmd = [
        "yt-dlp",
        "--download-sections", section_arg,
        "-f", format_selector,
        "--force-keyframes-at-cuts",
        "-o", str(final_output),
        "--no-playlist",
        "--merge-output-format", "mp4",
        url
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0 and final_output.exists() and final_output.stat().st_size > 1000:
            size_mb = round(final_output.stat().st_size / (1024 * 1024), 2)
            meta = {
                "file_path": str(final_output),
                "filename": out_filename,
                "size_mb": size_mb,
                "duration": clip_duration,
                "duration_str": seconds_to_time(clip_duration)
            }
            return True, f"Successfully created clip ({size_mb} MB)!", meta
        else:
            err_msg = proc.stderr.strip() if proc.stderr else proc.stdout.strip()
            return False, f"Trimming failed: {err_msg[:250]}", {}
    except subprocess.TimeoutExpired:
        return False, "Process timed out. Please select a shorter segment.", {}
    except Exception as e:
        return False, f"Unexpected error: {str(e)}", {}
