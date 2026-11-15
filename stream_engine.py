"""
StreamSlicer Engine - Unified Video Clipping & Trimming for Twitch.tv and Kick.com
Supports:
- Twitch VODs, Clips, Channels, and Livestreams
- Kick VODs, Clips, Channels, and Livestreams
- Automatic platform detection (Twitch vs Kick)
- Master resolution extraction (1080p60, 720p60, 480p, 360p, 160p)
- Direct stream playback preview URL generation (HLS / m3u8)
- Real-time progress percentage callback (0% - 100%) during clipping
- High-speed stream-copy slicing via yt-dlp & ffmpeg
"""

import os
import re
import json
import time
import datetime
import urllib.request
import urllib.parse
import subprocess
from pathlib import Path
from typing import Dict, Any, Tuple, Callable, Optional, List

KICK_API_CHANNEL = "https://kick.com/api/v2/channels/{slug}"
KICK_API_CHANNEL_VIDEOS = "https://kick.com/api/v2/channels/{slug}/videos"
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
    """Converts integer seconds into HH:MM:SS or MM:SS format."""
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# -------------------------------------------------------------
# PLATFORM DETECTION
# -------------------------------------------------------------
def detect_platform(raw_url: str) -> str:
    """Detects whether a URL or identifier belongs to Twitch or Kick."""
    u = raw_url.lower().strip()
    if "twitch.tv" in u or "twitch" in u:
        return "twitch"
    if "kick.com" in u or "kick" in u:
        return "kick"
    return "unknown"


# -------------------------------------------------------------
# RESOLUTION HELPERS
# -------------------------------------------------------------
def get_default_resolutions() -> List[Dict[str, Any]]:
    return [
        {"id": "best", "height": 9999, "label": "⚡ Best Available Source (Original)", "format_id": "best"},
        {"id": "1080p", "height": 1080, "label": "⚡ 1080p60 Full HD", "format_id": "1080p"},
        {"id": "720p", "height": 720, "label": "⚡ 720p60 HD", "format_id": "720p"},
        {"id": "480p", "height": 480, "label": "⚡ 480p SD", "format_id": "480p"},
        {"id": "360p", "height": 360, "label": "⚡ 360p", "format_id": "360p"},
        {"id": "160p", "height": 160, "label": "⚡ 160p (Data Saver)", "format_id": "160p"},
    ]


def parse_m3u8_resolutions(m3u8_url: str) -> List[Dict[str, Any]]:
    """Parses available resolutions directly from an HLS master playlist."""
    try:
        req = urllib.request.Request(m3u8_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=6) as resp:
            content = resp.read().decode("utf-8")
        
        resolutions = [
            {"id": "best", "height": 9999, "label": "⚡ Best Available Source (Original)", "format_id": "best"}
        ]
        
        matches = re.findall(r'RESOLUTION=(\d+)x(\d+)(?:,VIDEO="([^"]+)")?(?:,FRAME-RATE=([\d\.]+))?', content)
        seen = set()
        matches = sorted(matches, key=lambda m: int(m[1]), reverse=True)
        for w, h, vname, fps in matches:
            h_int = int(h)
            fps_int = int(float(fps)) if fps else 0
            fps_str = f"{fps_int}" if fps_int >= 50 else ""
            key = f"{h_int}p{fps_str}"
            if key in seen:
                continue
            seen.add(key)
            
            if h_int >= 1080:
                label = f"⚡ 1080p{fps_str} Full HD"
            elif h_int >= 720:
                label = f"⚡ 720p{fps_str} HD"
            elif h_int >= 480:
                label = f"⚡ 480p SD"
            else:
                label = f"⚡ {h_int}p"
                
            resolutions.append({
                "id": f"{h_int}p",
                "height": h_int,
                "label": label,
                "format_id": f"{h_int}p"
            })
            
        if len(resolutions) > 1:
            return resolutions
    except Exception as e:
        print(f"[StreamSlicer] m3u8 resolution parse notice: {e}")

    return get_default_resolutions()


# -------------------------------------------------------------
# TWITCH EXTRACTOR
# -------------------------------------------------------------
def clean_twitch_url(raw_url: str) -> str:
    """Standardizes input URLs into valid Twitch URLs."""
    url = raw_url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        if "/" in url or "twitch" in url:
            url = "https://" + url.lstrip("/")
        else:
            url = f"https://www.twitch.tv/{url.replace('@', '')}"
    return url


def resolve_twitch_url(url: str) -> str:
    """If user passed a Twitch channel root, attempt to resolve latest recorded VOD."""
    if "/videos/" in url or "/clip" in url or "clips.twitch.tv" in url:
        return url

    m = re.match(r'^https?://(?:www\.)?twitch\.tv/([a-zA-Z0-9_]+)/?$', url, re.IGNORECASE)
    if m:
        channel = m.group(1).lower()
        if channel not in ["directory", "downloads", "jobs", "p", "turbo", "search"]:
            archive_url = f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time"
            cmd = ["yt-dlp", "--dump-json", "--playlist-items", "1", archive_url]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
                if res.returncode == 0 and res.stdout.strip():
                    data = json.loads(res.stdout.strip().splitlines()[0])
                    vod_url = data.get("webpage_url")
                    if vod_url and "/videos/" in vod_url:
                        return vod_url
            except Exception as e:
                print(f"[StreamSlicer] Twitch channel VOD resolve warning: {e}")
    return url


def get_twitch_info(url: str) -> Dict[str, Any]:
    """Extracts Twitch video, clip, or stream metadata using yt-dlp."""
    target_url = resolve_twitch_url(clean_twitch_url(url))

    cmd = [
        "yt-dlp",
        "--dump-json",
        "--skip-download",
        "--no-warnings",
        "--no-playlist",
        target_url
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or "Twitch stream/video not found or unavailable."
            return {"success": False, "error": err_msg}

        data = json.loads(proc.stdout.strip().splitlines()[0])

        title = data.get("title") or "Twitch Video"
        uploader = data.get("uploader") or data.get("channel") or "Twitch Creator"
        duration = int(data.get("duration") or 0)
        thumbnail = data.get("thumbnail") or ""
        is_live = data.get("is_live", False)
        stream_id = str(data.get("id") or "")

        formats = data.get("formats", [])
        available_resolutions = [
            {"id": "best", "height": 9999, "label": "⚡ Best Available Source (Original)", "format_id": "best"}
        ]
        seen_res = set()

        preview_url = ""
        # 1. Filter formats for valid video resolutions & CORS-friendly video preview URL
        for f in formats:
            f_url = f.get("url") or ""
            fmt_id = str(f.get("format_id") or "").lower()
            vcodec = str(f.get("vcodec") or "").lower()
            h = f.get("height")

            if h and h not in seen_res:
                seen_res.add(h)
                fps = f.get("fps") or 0
                fps_str = f"{int(fps)}" if fps >= 50 else ""
                badge = "Full HD" if h >= 1080 else ("HD" if h >= 720 else ("SD" if h >= 480 else ""))
                badge_str = f" {badge}" if badge else ""
                label = f"⚡ {h}p{fps_str}{badge_str}".strip()

                available_resolutions.append({
                    "id": f"{h}p",
                    "height": h,
                    "label": label,
                    "format_id": f.get("format_id") or f"{h}p"
                })

            if "usher.ttvnw.net" in f_url or "audio_only" in fmt_id or "audio_only" in f_url.lower() or vcodec == "none":
                continue

            if ".m3u8" in f_url or ".mp4" in f_url:
                # Prefer 360p or 480p or first video stream for fast preview
                if not preview_url or (h and 360 <= h <= 480):
                    preview_url = f_url

        if not preview_url:
            raw_manifest = data.get("manifest_url") or data.get("url") or ""
            if "usher.ttvnw.net" not in raw_manifest:
                preview_url = raw_manifest

        available_resolutions = sorted(available_resolutions, key=lambda x: x["height"], reverse=True)
        if len(available_resolutions) <= 1:
            available_resolutions = get_default_resolutions()

        # Official Twitch embed player URL (runs in iframe with zero CORS restrictions)
        clean_stream_id = re.sub(r'^[a-zA-Z]+', '', stream_id)
        channel_name = re.sub(r'[^a-zA-Z0-9_]', '', uploader.lower())
        if is_live and channel_name:
            embed_url = f"https://player.twitch.tv/?channel={channel_name}&parent=streamslicer.pages.dev&parent=localhost&autoplay=false"
        elif clean_stream_id:
            embed_url = f"https://player.twitch.tv/?video={clean_stream_id}&parent=streamslicer.pages.dev&parent=localhost&autoplay=false"
        elif channel_name:
            embed_url = f"https://player.twitch.tv/?channel={channel_name}&parent=streamslicer.pages.dev&parent=localhost&autoplay=false"
        else:
            embed_url = ""

        return {
            "success": True,
            "platform": "twitch",
            "title": title,
            "uploader": uploader,
            "stream_id": stream_id,
            "duration": duration,
            "duration_str": seconds_to_time(duration) if duration else ("🔴 Live Stream" if is_live else "00:00:00"),
            "thumbnail": thumbnail,
            "is_live": is_live,
            "preview_url": preview_url,
            "available_resolutions": available_resolutions,
            "webpage_url": data.get("webpage_url", target_url),
            "source_m3u8": preview_url,
            "embed_url": embed_url
        }
    except Exception as e:
        return {"success": False, "error": f"Twitch fetch failed: {str(e)}"}


# -------------------------------------------------------------
# KICK EXTRACTOR
# -------------------------------------------------------------
def extract_video_uuid(url: str) -> str:
    """Extracts video UUID from URLs like kick.com/video/UUID or kick.com/channel/videos/UUID."""
    m = re.search(r'(?:video/|videos/)([a-zA-Z0-9_\-]+)', url)
    if m:
        return m.group(1)
    return ""


def clean_kick_url(raw: str) -> str:
    """Normalizes Kick URLs and handles bare channel names or missing scheme."""
    raw = raw.strip()
    if not raw.startswith("http://") and not raw.startswith("https://"):
        if "kick.com" in raw:
            raw = "https://" + raw.lstrip("/")
        else:
            raw = f"https://kick.com/{raw.replace('@', '')}"
    return raw


def extract_slug_from_url(url: str) -> str:
    """Extracts username slug or channel name from Kick URL."""
    url = url.strip()
    m = re.search(r'kick\.com/([a-zA-Z0-9_\-]+)', url, re.IGNORECASE)
    if m:
        slug = m.group(1).lower()
        if slug not in ['video', 'videos', 'categories', 'search', 'following', 'clips']:
            return slug
    return ""


def get_kick_vod_data(video_id: str) -> Dict[str, Any]:
    """Fetches Kick recorded VOD directly from Kick public API."""
    url = KICK_API_VIDEO.format(video_id=video_id)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            session = data.get("livestream", {})
            channel = session.get("channel", {})
            source_url = data.get("source") or session.get("source") or ""
            dur_ms = data.get("duration") or session.get("duration") or 0
            dur_sec = int(dur_ms / 1000) if dur_ms > 10000 else int(dur_ms)
            
            thumb = data.get("thumb") or (data.get("livestream") or {}).get("thumbnail") or data.get("thumbnail")
            if isinstance(thumb, dict):
                thumb = thumb.get("src") or thumb.get("url")

            resolutions = parse_m3u8_resolutions(source_url) if source_url else get_default_resolutions()

            return {
                "success": True,
                "platform": "kick",
                "title": session.get("session_title") or "Kick Broadcast",
                "uploader": channel.get("username") or channel.get("slug") or "Kick Streamer",
                "stream_id": str(data.get("id", video_id)),
                "duration": dur_sec,
                "duration_str": seconds_to_time(dur_sec),
                "thumbnail": thumb,
                "webpage_url": f"https://kick.com/video/{video_id}",
                "is_live": False,
                "preview_url": source_url,
                "available_resolutions": resolutions,
                "slug": channel.get("slug", ""),
                "source_m3u8": source_url,
                "embed_url": f"https://player.kick.com/{channel.get('slug')}" if channel.get("slug") else ""
            }
    except Exception as e:
        print(f"[StreamSlicer] Kick VOD API warning for '{video_id}': {e}")
        return {}


def get_kick_channel_data(slug: str) -> Dict[str, Any]:
    """Fetches public channel metadata from Kick v2 API."""
    slug = slug.strip().lower()
    if not slug:
        return {}
    url = KICK_API_CHANNEL.format(slug=slug)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"[StreamSlicer] Kick channel fetch warning: {e}")
        return {}


def get_kick_channel_videos(slug: str) -> List[Dict[str, Any]]:
    """Fetches public recorded VODs for a Kick channel."""
    slug = slug.strip().lower()
    if not slug:
        return []
    url = KICK_API_CHANNEL_VIDEOS.format(slug=slug)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data if isinstance(data, list) else []
    except Exception as e:
        return []


def extract_kick_via_ytdlp(target_url: str, slug: str = "") -> Dict[str, Any]:
    """Uses yt-dlp to extract high-accuracy Kick VOD or stream metadata."""
    cmd = ["yt-dlp", "--dump-json", "--skip-download", "--no-warnings", "--no-playlist", target_url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout.strip().splitlines()[0])
            duration = int(data.get("duration") or 0)
            formats = data.get("formats", [])
            preview_url = data.get("manifest_url") or data.get("url") or ""

            # Check formats for clean .m3u8
            for f in formats:
                f_url = f.get("url") or ""
                if ".m3u8" in f_url and "d26yk4zpyhjeeq.cloudfront.net" in f_url:
                    preview_url = f_url
                    break

            available_resolutions = [
                {"id": "best", "height": 9999, "label": "⚡ Best Available Source (Original)", "format_id": "best"}
            ]
            seen_res = set()
            for f in formats:
                h = f.get("height")
                if h and h not in seen_res:
                    seen_res.add(h)
                    fps = f.get("fps") or 0
                    fps_str = f"{int(fps)}" if fps >= 50 else ""
                    badge = "Full HD" if h >= 1080 else ("HD" if h >= 720 else ("SD" if h >= 480 else ""))
                    badge_str = f" {badge}" if badge else ""
                    available_resolutions.append({
                        "id": f"{h}p",
                        "height": h,
                        "label": f"⚡ {h}p{fps_str}{badge_str}".strip(),
                        "format_id": f.get("format_id") or f"{h}p"
                    })

            available_resolutions = sorted(available_resolutions, key=lambda x: x["height"], reverse=True)
            if len(available_resolutions) <= 1:
                available_resolutions = get_default_resolutions()

            thumb = data.get("thumbnail") or ""
            uploader = data.get("uploader") or data.get("channel") or slug or "Kick Streamer"
            clean_slug = slug or uploader.lower().replace(" ", "")

            # If thumbnail is missing, fetch channel avatar
            if not thumb and clean_slug:
                try:
                    ch_data = get_kick_channel_data(clean_slug)
                    thumb = ch_data.get("user", {}).get("profile_pic") or ""
                except Exception:
                    pass

            return {
                "success": True,
                "platform": "kick",
                "title": data.get("title") or data.get("fulltitle") or "Kick Video",
                "uploader": uploader,
                "stream_id": str(data.get("id") or "N/A"),
                "duration": duration,
                "duration_str": seconds_to_time(duration) if duration else "00:00:00",
                "thumbnail": thumb,
                "webpage_url": data.get("webpage_url") or target_url,
                "is_live": data.get("is_live", False),
                "preview_url": preview_url,
                "available_resolutions": available_resolutions,
                "slug": clean_slug,
                "source_m3u8": preview_url,
                "embed_url": f"https://player.kick.com/{clean_slug}" if clean_slug else ""
            }
    except Exception as e:
        print(f"[StreamSlicer] yt-dlp kick extraction warning: {e}")
    return {}


def get_kick_info(url_or_channel: str) -> Dict[str, Any]:
    """Extracts Kick VOD, livestream, or channel info."""
    target = clean_kick_url(url_or_channel)
    is_video_url = ("/videos/" in target or "/video/" in target)
    video_uuid = extract_video_uuid(target)
    slug = extract_slug_from_url(target)

    # 1. If user passed a specific VOD URL or UUID
    if is_video_url or video_uuid:
        if video_uuid:
            vod_data = get_kick_vod_data(video_uuid)
            if vod_data and vod_data.get("success") and vod_data.get("duration", 0) > 0:
                return vod_data
        # If API v1 failed or returned 0 duration, extract directly via yt-dlp
        yt_vod = extract_kick_via_ytdlp(target, slug)
        if yt_vod and yt_vod.get("success") and yt_vod.get("duration", 0) > 0:
            return yt_vod

    # 2. Channel live stream or recent broadcasts
    channel_api_data = {}
    if slug:
        channel_api_data = get_kick_channel_data(slug)

    livestream = channel_api_data.get("livestream") or {}
    stream_id = livestream.get("id") or channel_api_data.get("id") or "N/A"
    is_live = livestream.get("is_live", False) if isinstance(livestream, dict) else False

    # If channel is currently LIVE
    if is_live and livestream:
        preview_url = channel_api_data.get("playback_url") or livestream.get("source") or ""
        livestream_thumb = livestream.get("thumbnail")
        thumb = livestream_thumb.get("url") if isinstance(livestream_thumb, dict) else (livestream_thumb or livestream.get("thumb"))
        dur = livestream.get("duration") or 0
        resolutions = parse_m3u8_resolutions(preview_url) if preview_url else get_default_resolutions()

        return {
            "success": True,
            "platform": "kick",
            "title": livestream.get("session_title") or f"{slug.upper()}'s Live Stream",
            "uploader": channel_api_data.get("user", {}).get("username") or slug,
            "stream_id": str(stream_id),
            "duration": dur,
            "duration_str": "🔴 Live Stream",
            "thumbnail": thumb,
            "webpage_url": target,
            "is_live": True,
            "preview_url": preview_url,
            "available_resolutions": resolutions,
            "slug": slug,
            "source_m3u8": preview_url,
            "embed_url": f"https://player.kick.com/{slug}" if slug else ""
        }

    # If channel is OFFLINE, fetch most recent recorded VOD
    if slug:
        recent_videos = get_kick_channel_videos(slug)
        if recent_videos:
            latest_v = recent_videos[0]
            vid_uuid = latest_v.get("video", {}).get("uuid") if isinstance(latest_v.get("video"), dict) else None
            if not vid_uuid and latest_v.get("uuid"):
                vid_uuid = latest_v.get("uuid")

            if vid_uuid:
                vod_data = get_kick_vod_data(vid_uuid)
                if vod_data and vod_data.get("success") and vod_data.get("duration", 0) > 0:
                    vod_data["title"] = f"{vod_data['title']} (Latest Broadcast)"
                    return vod_data

            source_url = latest_v.get("source") or ""
            dur_ms = latest_v.get("duration") or 0
            dur_sec = int(dur_ms / 1000) if dur_ms > 10000 else int(dur_ms)
            thumb = latest_v.get("thumbnail")
            if isinstance(thumb, dict):
                thumb = thumb.get("src") or thumb.get("url")
            elif not thumb:
                thumb = (latest_v.get("livestream") or {}).get("thumbnail") or latest_v.get("thumb")

            if source_url and dur_sec > 0:
                return {
                    "success": True,
                    "platform": "kick",
                    "title": latest_v.get("session_title") or f"{slug.upper()}'s Recent Broadcast",
                    "uploader": channel_api_data.get("user", {}).get("username") or slug,
                    "stream_id": str(latest_v.get("id") or stream_id),
                    "duration": dur_sec,
                    "duration_str": seconds_to_time(dur_sec),
                    "thumbnail": thumb,
                    "webpage_url": target,
                    "is_live": False,
                    "preview_url": source_url,
                    "available_resolutions": parse_m3u8_resolutions(source_url) if source_url else get_default_resolutions(),
                    "slug": slug,
                    "source_m3u8": source_url,
                    "embed_url": f"https://player.kick.com/{slug}" if slug else ""
                }

    # 4. Final Fallback to yt-dlp metadata
    yt_res = extract_kick_via_ytdlp(target, slug)
    if yt_res and yt_res.get("success"):
        return yt_res

    return {
        "success": False,
        "error": "Could not fetch Kick video or channel information. Please verify the URL."
    }


# -------------------------------------------------------------
# UNIFIED GET_STREAM_INFO
# -------------------------------------------------------------
def get_stream_info(raw_url: str) -> Dict[str, Any]:
    """Unified video info router that auto-detects Twitch vs Kick."""
    url = raw_url.strip()
    if not url:
        return {"success": False, "error": "Stream or VOD URL is required."}

    platform = detect_platform(url)
    if platform == "twitch":
        return get_twitch_info(url)
    elif platform == "kick":
        return get_kick_info(url)
    else:
        # Fallback: try Twitch first, then Kick
        tw = get_twitch_info(url)
        if tw.get("success"):
            return tw
        return get_kick_info(url)


# -------------------------------------------------------------
# UNIFIED VIDEO CLIPPER WITH STREAM-COPY & PROGRESS
# -------------------------------------------------------------
def clip_stream_video(
    url: str,
    start_time: str,
    end_time: str,
    quality: str = "best",
    output_dir: str = "/tmp/streamslicer",
    progress_callback: Optional[Callable[[float, str], None]] = None
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Clips any Twitch or Kick stream / VOD segment.
    Uses ultra-fast keyframe stream-copy slicing with real-time percentage progress.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    start_sec = time_to_seconds(start_time)
    end_sec = time_to_seconds(end_time)

    if end_sec <= start_sec:
        return False, "End time must be greater than Start time.", {}

    clip_duration = end_sec - start_sec
    if clip_duration > 7200:  # 2 hours max
        return False, "Clip duration cannot exceed 2 hours at a time.", {}

    platform = detect_platform(url)
    prefix = "twitch" if platform == "twitch" else ("kick" if platform == "kick" else "clip")
    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_filename = f"streamslicer_{prefix}_{timestamp_str}.mp4"
    final_output = out_path / out_filename

    target_media = url.strip()
    if platform == "twitch":
        target_media = resolve_twitch_url(clean_twitch_url(target_media))
    elif platform == "kick":
        if not target_media.endswith(".m3u8"):
            resolved_info = get_kick_info(target_media)
            if resolved_info.get("success") and resolved_info.get("source_m3u8"):
                target_media = resolved_info["source_m3u8"]

    # Format quality selector for yt-dlp - prioritizes crisp source streams and highest bitrates
    q_lower = (quality or "best").lower().strip()

    # Intelligent Kick / HLS variant playlist resolution:
    # If target is an HLS master playlist, resolve the exact sub-playlist matching requested resolution
    is_variant_stream = False
    if ".m3u8" in target_media and "master.m3u8" in target_media:
        try:
            req_h = 1080 if ("1080" in q_lower or q_lower in ["best", "source", "original"]) else (
                720 if "720" in q_lower else (
                    480 if "480" in q_lower else (
                        360 if "360" in q_lower else (
                            160 if "160" in q_lower else None
                        )
                    )
                )
            )
            if req_h:
                req = urllib.request.Request(target_media, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                with urllib.request.urlopen(req, timeout=5) as resp:
                    m_content = resp.read().decode("utf-8", errors="ignore")
                p_matches = re.findall(r'#EXT-X-STREAM-INF:[^\n]*RESOLUTION=(\d+)x(\d+)[^\n]*\n([^\r\n]+)', m_content)
                chosen_uri = None
                for w_str, h_str, u_str in p_matches:
                    if int(h_str) == req_h:
                        chosen_uri = u_str.strip()
                        break
                if not chosen_uri and p_matches:
                    sorted_p = sorted(p_matches, key=lambda x: int(x[1]), reverse=True)
                    for w_str, h_str, u_str in sorted_p:
                        if int(h_str) <= req_h:
                            chosen_uri = u_str.strip()
                            break
                if chosen_uri:
                    target_media = urllib.parse.urljoin(target_media, chosen_uri)
                    is_variant_stream = True
        except Exception as e:
            print(f"[StreamSlicer] Master playlist variant resolve notice: {e}")

    # Format quality selector for yt-dlp
    if is_variant_stream:
        # Direct variant playlist already isolates the requested stream
        format_selector = "best"
    elif q_lower in ["best", "source", "original"]:
        format_selector = "1080p60__source_/1080p60/chunked/best"
    elif "1080" in q_lower:
        format_selector = "1080p60__source_/1080p60/1080p/best[height<=1080]/best"
    elif "720" in q_lower:
        format_selector = "720p60/720p30/720p/best[height<=720]"
    elif "480" in q_lower:
        format_selector = "480p30/480p/best[height<=480]"
    elif "360" in q_lower:
        format_selector = "360p30/360p/best[height<=360]"
    elif "160" in q_lower:
        format_selector = "160p30/160p/best[height<=160]"
    else:
        format_selector = f"{quality}/best"

    section_arg = f"*{seconds_to_time(start_sec)}-{seconds_to_time(end_sec)}"

    extra_headers = []
    if platform == "kick" or "kick" in target_media.lower() or "live-video.net" in target_media.lower():
        extra_headers = [
            "--add-header", "Referer:https://kick.com/",
            "--add-header", "Origin:https://kick.com",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ]

    cmd = [
        "yt-dlp",
        "--download-sections", section_arg,
        "-f", format_selector,
        "-o", str(final_output),
        "--no-playlist",
        "--merge-output-format", "mp4",
        "--newline",
    ] + extra_headers + [target_media]

    if progress_callback:
        progress_callback(8.0, f"Connecting to {platform.capitalize() if platform != 'unknown' else 'stream'} servers...")

    last_pct = 8.0

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        recent_lines = []
        for raw_line in iter(proc.stdout.readline, ''):
            line_str = raw_line.strip()
            if not line_str:
                continue

            recent_lines.append(line_str)
            if len(recent_lines) > 25:
                recent_lines.pop(0)

            # Step 1: Range located
            if "[info]" in line_str and "Downloading 1 time ranges" in line_str:
                if last_pct < 16.0:
                    last_pct = 16.0
                    if progress_callback:
                        progress_callback(16.0, "Locating video stream segments...")

            # Step 2: Opening chunks
            elif "Opening 'https://" in line_str:
                if last_pct < 24.0:
                    last_pct = 24.0
                    if progress_callback:
                        progress_callback(24.0, "Connecting to stream chunks...")

            # Step 3: Stream mapping
            elif "Stream mapping:" in line_str or "Input #0" in line_str:
                if last_pct < 30.0:
                    last_pct = 30.0
                    if progress_callback:
                        progress_callback(30.0, "Stream connected, slicing keyframes...")

            # Step 4: FFmpeg time progress
            m_time = re.search(r'time=(\d+):(\d+):([\d\.]+)', line_str)
            if m_time:
                sec = int(m_time.group(1)) * 3600 + int(m_time.group(2)) * 60 + float(m_time.group(3))
                if clip_duration > 0:
                    ratio = min(1.0, max(0.0, sec / clip_duration))
                    calc_pct = 30.0 + (ratio * 65.0)
                    if calc_pct > last_pct:
                        last_pct = round(calc_pct, 1)
                        if progress_callback:
                            progress_callback(last_pct, f"Slicing clip: {last_pct}% ({seconds_to_time(int(sec))}/{seconds_to_time(clip_duration)})")
                continue

            # Step 5: yt-dlp percentage
            m_dl = re.search(r'\[download\]\s+([\d\.]+)%', line_str)
            if m_dl:
                try:
                    val = float(m_dl.group(1))
                    ratio = min(1.0, max(0.0, val / 100.0))
                    calc_pct = 30.0 + (ratio * 65.0)
                    if calc_pct > last_pct:
                        last_pct = round(calc_pct, 1)
                        if progress_callback:
                            progress_callback(last_pct, f"Downloading stream segments: {last_pct}%")
                except ValueError:
                    pass
                continue

            # Step 6: Fixup & packaging
            if "[FixupM3u8]" in line_str or "Fixing" in line_str or "Merging" in line_str:
                if last_pct < 96.0:
                    last_pct = 96.0
                    if progress_callback:
                        progress_callback(96.0, "Packaging MP4 clip container...")

        proc.wait(timeout=600)

        # Retry loop to verify file flushed to disk
        resolved_file = None
        for _ in range(10):
            if final_output.exists() and final_output.stat().st_size > 1000:
                resolved_file = final_output
                break
            candidates = list(out_path.glob(f"streamslicer_{prefix}_{timestamp_str}*"))
            for cand in candidates:
                if cand.exists() and cand.stat().st_size > 1000:
                    resolved_file = cand
                    break
            if resolved_file:
                break
            recent_mp4s = sorted(
                [f for f in out_path.glob(f"streamslicer_{prefix}_*.mp4") if f.exists() and f.stat().st_size > 1000 and (time.time() - f.stat().st_mtime) < 90],
                key=lambda x: x.stat().st_mtime,
                reverse=True
            )
            if recent_mp4s:
                resolved_file = recent_mp4s[0]
                break
            time.sleep(0.5)

        # Clean corrupted zero-byte files
        if final_output.exists() and final_output.stat().st_size < 1000:
            try:
                final_output.unlink(missing_ok=True)
            except Exception:
                pass

        if proc.returncode == 0 and resolved_file:
            if progress_callback:
                progress_callback(100.0, "100% Complete! Ready for download.")
            size_mb = round(resolved_file.stat().st_size / (1024 * 1024), 2)
            meta = {
                "file_path": str(resolved_file),
                "filename": resolved_file.name,
                "size_mb": size_mb,
                "duration": clip_duration,
                "duration_str": seconds_to_time(clip_duration),
                "platform": platform
            }
            return True, f"Successfully created clip ({size_mb} MB)!", meta
        else:
            err_hints = [l for l in recent_lines if any(k in l.lower() for k in ["error", "warning", "fail", "unable"])]
            if err_hints:
                err_msg = " | ".join(err_hints[-2:])
            elif proc.returncode == 0:
                err_msg = "Selected timestamp produced no video data. Please verify start and end timestamps."
            else:
                err_msg = f"Video segment could not be processed (exit code {proc.returncode})"
            return False, f"Trimming failed: {err_msg}", {}

    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
        return False, "Process timed out. For longer clips, please try a segment under 30 minutes.", {}
    except Exception as e:
        return False, f"Unexpected error: {str(e)}", {}

