"""
KickCutter Engine - Video Clipping & Trimming for Kick.com
Supports:
- Kick VODs, Clips, and Livestream segment extraction.
- Automatic extraction of video resolutions (1080p, 720p, 480p, etc.).
- Direct stream playback preview URL generation (HLS / m3u8).
- Real-time progress percentage callback (0% - 100%) during clipping.
- High-speed slicing via yt-dlp and ffmpeg.
"""

import os
import re
import json
import datetime
import urllib.request
import subprocess
from pathlib import Path
from typing import Dict, Any, Tuple, Callable, Optional, List

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


def get_default_resolutions() -> List[Dict[str, Any]]:
    return [
        {"id": "best", "height": 9999, "label": "Best Available Source (Original)", "format_id": "best"},
        {"id": "1080p", "height": 1080, "label": "1080p Full HD (Highest)", "format_id": "1080p"},
        {"id": "720p", "height": 720, "label": "720p HD (Balanced & Fast)", "format_id": "720p"},
        {"id": "480p", "height": 480, "label": "480p SD (Lightweight)", "format_id": "480p"},
        {"id": "360p", "height": 360, "label": "360p (Data Saver)", "format_id": "360p"},
    ]


def parse_available_resolutions(raw_formats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extracts distinct available video resolutions from yt-dlp formats array."""
    if not raw_formats:
        return get_default_resolutions()

    resolutions = []
    seen = set()

    # Always add 'best' option first
    resolutions.append({
        "id": "best",
        "height": 9999,
        "label": "Best Available Source (Original Quality)",
        "format_id": "best"
    })

    # Sort formats from highest height to lowest
    sorted_formats = sorted(raw_formats, key=lambda f: f.get("height") or 0, reverse=True)

    for f in sorted_formats:
        h = f.get("height")
        if not h or h <= 0:
            continue
        
        fid = f.get("format_id") or str(h)
        fps = f.get("fps")
        fps_str = f"{int(fps)}" if fps and fps >= 50 else ""
        
        res_key = f"{h}p{fps_str}"
        if res_key in seen:
            continue
        seen.add(res_key)

        if h >= 1080:
            label = f"1080p{fps_str} Full HD"
        elif h >= 720:
            label = f"720p{fps_str} HD"
        elif h >= 480:
            label = f"480p SD"
        elif h >= 360:
            label = f"360p"
        elif h >= 160:
            label = f"160p"
        else:
            label = f"{h}p"

        resolutions.append({
            "id": fid if fid.endswith("p") or "p" in fid else f"{h}p",
            "height": h,
            "label": label,
            "format_id": fid
        })

    return resolutions if len(resolutions) > 1 else get_default_resolutions()


def get_kick_info(url_or_channel: str) -> Dict[str, Any]:
    """
    Extracts complete info (Title, Uploader, Duration, Stream ID, Thumbnail,
    Available Resolutions, and Stream Preview URL) using yt-dlp and Kick public APIs.
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

    # 2. Query yt-dlp for video metadata and available stream formats
    cmd = ["yt-dlp", "--dump-json", "--no-warnings", target]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            info = json.loads(result.stdout.strip().splitlines()[0])
            duration = info.get("duration") or 0
            formats = info.get("formats") or []
            resolutions = parse_available_resolutions(formats)
            
            # Preview URL priority: manifest_url -> direct stream url -> channel playback_url
            preview_url = info.get("manifest_url") or info.get("url") or ""
            if not preview_url and channel_api_data:
                preview_url = channel_api_data.get("playback_url") or (livestream.get("source") if isinstance(livestream, dict) else "")

            thumb = info.get("thumbnail")
            if not thumb and livestream:
                livestream_thumb = livestream.get("thumbnail")
                thumb = livestream_thumb.get("url") if isinstance(livestream_thumb, dict) else livestream_thumb

            return {
                "success": True,
                "title": info.get("title") or info.get("fulltitle") or (livestream.get("session_title") if livestream else f"{slug.upper()}'s Kick Stream"),
                "uploader": info.get("uploader") or info.get("channel") or (channel_api_data.get("user", {}).get("username") if channel_api_data else slug),
                "stream_id": str(info.get("display_id") or stream_id),
                "duration": duration,
                "duration_str": seconds_to_time(duration) if duration else ("Live Stream" if info.get("is_live") else "00:00:00"),
                "thumbnail": thumb,
                "webpage_url": info.get("webpage_url") or target,
                "is_live": info.get("is_live", False),
                "preview_url": preview_url,
                "available_resolutions": resolutions,
                "slug": slug
            }
    except Exception as e:
        print(f"[Engine] yt-dlp metadata error: {e}")

    # Fallback to channel API data if yt-dlp was slow or timed out
    if channel_api_data:
        preview_url = channel_api_data.get("playback_url") or (livestream.get("source") if isinstance(livestream, dict) else "")
        livestream_thumb = livestream.get("thumbnail")
        thumb = livestream_thumb.get("url") if isinstance(livestream_thumb, dict) else livestream_thumb

        return {
            "success": True,
            "title": livestream.get("session_title") or f"{slug.upper()}'s Kick Stream",
            "uploader": channel_api_data.get("user", {}).get("username") or slug,
            "stream_id": str(stream_id),
            "duration": livestream.get("duration") or 0,
            "duration_str": "Live Stream" if livestream.get("is_live") else "Recorded VOD",
            "thumbnail": thumb,
            "webpage_url": target,
            "is_live": livestream.get("is_live", False),
            "preview_url": preview_url,
            "available_resolutions": get_default_resolutions(),
            "slug": slug
        }

    return {
        "success": False,
        "error": "Could not fetch Kick video or channel information. Please verify the URL."
    }


# -------------------------------------------------------------
# VIDEO CLIPPER WITH REAL-TIME PROGRESS TRACKING
# -------------------------------------------------------------
def clip_kick_video(
    url: str,
    start_time: str,
    end_time: str,
    quality: str = "720p",
    output_dir: str = "/tmp/kickcuttr",
    progress_callback: Optional[Callable[[float, str], None]] = None
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Clips a Kick VOD or stream segment between start_time and end_time.
    Uses yt-dlp with --download-sections and pipes real-time progress percentage (0-100%).
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
    q_lower = quality.lower().strip()
    if q_lower == "best":
        format_selector = "bestvideo+bestaudio/best"
    elif "1080" in q_lower:
        format_selector = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
    elif "720" in q_lower:
        format_selector = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
    elif "480" in q_lower:
        format_selector = "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
    elif "360" in q_lower:
        format_selector = "bestvideo[height<=360]+bestaudio/best[height<=360]/best"
    elif "160" in q_lower:
        format_selector = "bestvideo[height<=160]+bestaudio/best[height<=160]/best"
    else:
        # User picked a specific format_id
        format_selector = f"{quality}/bestvideo+bestaudio/best"

    section_arg = f"*{seconds_to_time(start_sec)}-{seconds_to_time(end_sec)}"

    cmd = [
        "yt-dlp",
        "--download-sections", section_arg,
        "-f", format_selector,
        "--force-keyframes-at-cuts",
        "-o", str(final_output),
        "--no-playlist",
        "--merge-output-format", "mp4",
        "--newline",
        url
    ]

    if progress_callback:
        progress_callback(5.0, "Connecting to stream server...")

    last_pct = 5.0

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        for line in proc.stdout:
            line_str = line.strip()
            if not line_str:
                continue

            # Check for ffmpeg transcoding time (e.g. time=00:00:11.93)
            m_time = re.search(r'time=(\d+):(\d+):([\d\.]+)', line_str)
            if m_time:
                sec = int(m_time.group(1)) * 3600 + int(m_time.group(2)) * 60 + float(m_time.group(3))
                if clip_duration > 0:
                    calc_pct = min(98.0, (sec / clip_duration) * 100.0)
                    if calc_pct > last_pct:
                        last_pct = round(calc_pct, 1)
                        if progress_callback:
                            progress_callback(last_pct, f"Slicing clip: {last_pct}%")
                continue

            # Check for yt-dlp download percentage (e.g. [download]  45.0% of ...)
            m_dl = re.search(r'\[download\]\s+([\d\.]+)%', line_str)
            if m_dl:
                try:
                    val = float(m_dl.group(1))
                    calc_pct = min(98.0, val)
                    if calc_pct > last_pct:
                        last_pct = round(calc_pct, 1)
                        if progress_callback:
                            progress_callback(last_pct, f"Downloading stream segments: {last_pct}%")
                except ValueError:
                    pass
                continue

            # Check for container fixup / final packaging
            if "[FixupM3u8]" in line_str or "Fixing" in line_str or "Merging" in line_str:
                if last_pct < 96.0:
                    last_pct = 96.0
                    if progress_callback:
                        progress_callback(96.0, "Packaging MP4 clip...")

        proc.wait(timeout=300)

        if proc.returncode == 0 and final_output.exists() and final_output.stat().st_size > 1000:
            if progress_callback:
                progress_callback(100.0, "100% Complete! Ready for download.")
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
            return False, "Trimming failed: video segment could not be generated. Please try a different time range or resolution.", {}

    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
        return False, "Process timed out. Please select a shorter segment.", {}
    except Exception as e:
        return False, f"Unexpected error: {str(e)}", {}
