"""
KickCutter Engine - Video Clipping & Trimming for Kick.com
Supports:
- Kick VODs (kick.com/video/UUID or kick.com/channel/videos/UUID)
- Direct Kick Channels & Livestreams
- Automatic extraction of video resolutions (1080p, 720p, 480p, 360p, 160p)
- Direct stream playback preview URL generation (HLS / m3u8) with CORS support
- Real-time progress percentage callback (0% - 100%) during clipping
- High-speed slicing via yt-dlp and ffmpeg
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
def extract_video_uuid(url: str) -> str:
    """Extracts video UUID from URLs like kick.com/video/UUID or kick.com/channel/videos/UUID."""
    m = re.search(r'(?:video/|videos/)([a-zA-Z0-9_\-]+)', url)
    if m:
        return m.group(1)
    return ""


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


def get_kick_channel_videos(slug: str) -> List[Dict[str, Any]]:
    """Fetches public recorded VODs for a channel."""
    slug = slug.strip().lower()
    if not slug:
        return []
    url = KICK_API_CHANNEL_VIDEOS.format(slug=slug)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[Engine] Channel videos fetch warning for '{slug}': {e}")
        return []


def get_default_resolutions() -> List[Dict[str, Any]]:
    return [
        {"id": "best", "height": 9999, "label": "Best Available Source (Original)", "format_id": "best"},
        {"id": "1080p", "height": 1080, "label": "1080p60 Full HD", "format_id": "1080p"},
        {"id": "720p", "height": 720, "label": "720p60 HD", "format_id": "720p"},
        {"id": "480p", "height": 480, "label": "480p SD", "format_id": "480p"},
        {"id": "360p", "height": 360, "label": "360p", "format_id": "360p"},
        {"id": "160p", "height": 160, "label": "160p (Data Saver)", "format_id": "160p"},
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
            {"id": "best", "height": 9999, "label": "⚡ Best Available Source (Original Quality)", "format_id": "best"}
        ]
        
        matches = re.findall(r'RESOLUTION=(\d+)x(\d+)(?:,VIDEO="([^"]+)")?(?:,FRAME-RATE=([\d\.]+))?', content)
        seen = set()
        
        # Sort descending by height
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
        print(f"[Engine] Could not parse m3u8 resolutions: {e}")
        
    return get_default_resolutions()


def get_kick_vod_data(video_uuid: str) -> Optional[Dict[str, Any]]:
    """Fetches VOD information directly from Kick API v1."""
    url = KICK_API_VIDEO.format(video_id=video_uuid)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            livestream = data.get("livestream") or {}
            channel = livestream.get("channel") or {}
            
            # Duration in seconds (Kick API returns ms)
            dur_ms = livestream.get("duration") or 0
            dur_sec = int(dur_ms / 1000) if dur_ms > 10000 else int(dur_ms)
            
            thumb = None
            if livestream.get("thumbnail"):
                t = livestream["thumbnail"]
                thumb = t.get("src") if isinstance(t, dict) else t
            elif data.get("thumb"):
                thumb = data.get("thumb")
            
            source = data.get("source") # master.m3u8
            resolutions = parse_m3u8_resolutions(source) if source else get_default_resolutions()
            
            creator_name = channel.get("user", {}).get("username") or channel.get("slug") or "Creator"
            title = livestream.get("session_title") or f"{creator_name}'s Kick VOD"
            
            return {
                "success": True,
                "title": title,
                "uploader": creator_name,
                "stream_id": str(data.get("id") or livestream.get("id") or video_uuid),
                "duration": dur_sec,
                "duration_str": seconds_to_time(dur_sec),
                "thumbnail": thumb,
                "preview_url": source,
                "available_resolutions": resolutions,
                "is_live": False,
                "slug": channel.get("slug", ""),
                "webpage_url": f"https://kick.com/video/{video_uuid}",
                "source_m3u8": source
            }
    except Exception as e:
        print(f"[Engine] Error fetching VOD {video_uuid}: {e}")
        return None


def get_twitch_metadata(target_url: str) -> Dict[str, Any]:
    """Extracts Twitch video, VOD, clip or livestream metadata directly using yt-dlp."""
    target_url = target_url.strip()
    if not target_url.startswith("http://") and not target_url.startswith("https://"):
        target_url = "https://" + target_url.lstrip("/")
    
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--skip-download",
        "--no-warnings",
        "--no-playlist",
        target_url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip().splitlines()[0])
            title = data.get("title") or data.get("fulltitle") or "Twitch Stream"
            uploader = data.get("uploader") or data.get("channel") or data.get("creator") or "Streamer"
            duration = data.get("duration") or 0
            stream_id = str(data.get("id") or data.get("display_id") or "N/A")
            thumbnail = data.get("thumbnail") or ""
            is_live = data.get("is_live", False)

            formats = data.get("formats", [])
            available_resolutions = []
            seen_heights = set()

            for f in formats:
                h = f.get("height")
                fid = f.get("format_id", "")
                if h and h not in seen_heights:
                    seen_heights.add(h)
                    fps = f.get("fps") or 30
                    fps_label = "60" if fps > 45 else ""
                    badge = "Full HD" if h >= 1080 else ("HD" if h >= 720 else ("SD" if h >= 480 else ""))
                    badge_str = f" {badge}" if badge else ""
                    res_label = f"⚡ {h}p{fps_label}{badge_str}".strip()
                    available_resolutions.append({
                        "id": f"{h}p{fps_label}".strip(),
                        "height": h,
                        "label": res_label,
                        "format_id": fid
                    })

            available_resolutions = sorted(available_resolutions, key=lambda x: x["height"], reverse=True)
            if not available_resolutions:
                available_resolutions = get_default_resolutions()

            return {
                "success": True,
                "title": title,
                "uploader": uploader,
                "stream_id": stream_id,
                "duration": int(duration),
                "duration_str": seconds_to_time(int(duration)) if duration else "🔴 Live Stream",
                "thumbnail": thumbnail,
                "is_live": is_live,
                "preview_url": data.get("manifest_url") or data.get("url") or "",
                "available_resolutions": available_resolutions,
                "slug": data.get("channel") or uploader,
                "webpage_url": data.get("webpage_url", target_url),
                "source_m3u8": ""  # keep empty so clip_kick_video uses target_url directly!
            }
    except Exception as e:
        print(f"[Engine] Twitch metadata fetch error: {e}")

    return {
        "success": False,
        "error": "Could not fetch Twitch stream information. Please verify the URL."
    }


def get_kick_info(url_or_channel: str) -> Dict[str, Any]:
    """
    Extracts complete info (Title, Uploader, Duration, Stream ID, Thumbnail,
    Available Resolutions, and Stream Preview URL) for Kick and Twitch VODs, Channels, and Streams.
    """
    target = url_or_channel.strip()

    # Route Twitch URLs directly to Twitch metadata extractor (instant, no Kick timeouts!)
    if "twitch.tv" in target.lower() or "twitch" in target.lower():
        return get_twitch_metadata(target)

    # 1. Check if the input is a direct Kick VOD URL (kick.com/video/UUID or /videos/UUID)
    video_uuid = extract_video_uuid(target)
    if video_uuid:
        vod_data = get_kick_vod_data(video_uuid)
        if vod_data and vod_data.get("success"):
            return vod_data

    # 2. Extract Channel Slug
    if not target.startswith("http"):
        slug = target.replace("@", "")
        target = f"https://kick.com/{slug}"
    else:
        slug = extract_slug_from_url(target)

    # 3. Fetch channel data
    channel_api_data = {}
    if slug:
        channel_api_data = get_kick_channel_data(slug)

    livestream = channel_api_data.get("livestream") or {}
    stream_id = livestream.get("id") or channel_api_data.get("id") or "N/A"
    is_live = livestream.get("is_live", False) if isinstance(livestream, dict) else False

    # If channel is currently LIVE, return livestream details
    if is_live and livestream:
        preview_url = channel_api_data.get("playback_url") or livestream.get("source") or ""
        livestream_thumb = livestream.get("thumbnail")
        thumb = livestream_thumb.get("url") if isinstance(livestream_thumb, dict) else livestream_thumb
        dur = livestream.get("duration") or 0
        resolutions = parse_m3u8_resolutions(preview_url) if preview_url else get_default_resolutions()

        return {
            "success": True,
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
            "source_m3u8": preview_url
        }

    # If channel is OFFLINE, fetch their most recent recorded VOD so user can clip it!
    if slug:
        recent_videos = get_kick_channel_videos(slug)
        if recent_videos:
            latest_v = recent_videos[0]
            vid_uuid = latest_v.get("video", {}).get("uuid") if isinstance(latest_v.get("video"), dict) else None
            if not vid_uuid and latest_v.get("uuid"):
                vid_uuid = latest_v.get("uuid")

            if vid_uuid:
                vod_data = get_kick_vod_data(vid_uuid)
                if vod_data and vod_data.get("success"):
                    vod_data["title"] = f"{vod_data['title']} (Latest Broadcast)"
                    return vod_data

            # Direct fallback from latest video item
            source_url = latest_v.get("source") or ""
            dur_ms = latest_v.get("duration") or 0
            dur_sec = int(dur_ms / 1000) if dur_ms > 10000 else int(dur_ms)
            thumb = latest_v.get("thumbnail")
            if isinstance(thumb, dict):
                thumb = thumb.get("src") or thumb.get("url")

            return {
                "success": True,
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
                "source_m3u8": source_url
            }

    # 4. Fallback to yt-dlp metadata
    cmd = ["yt-dlp", "--dump-json", "--no-warnings", target]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            info = json.loads(result.stdout.strip().splitlines()[0])
            duration = info.get("duration") or 0
            preview_url = info.get("manifest_url") or info.get("url") or ""
            return {
                "success": True,
                "title": info.get("title") or info.get("fulltitle") or "Kick Stream",
                "uploader": info.get("uploader") or info.get("channel") or slug or "Creator",
                "stream_id": str(info.get("display_id") or stream_id),
                "duration": duration,
                "duration_str": seconds_to_time(duration) if duration else "00:00:00",
                "thumbnail": info.get("thumbnail"),
                "webpage_url": info.get("webpage_url") or target,
                "is_live": info.get("is_live", False),
                "preview_url": preview_url,
                "available_resolutions": parse_m3u8_resolutions(preview_url) if preview_url else get_default_resolutions(),
                "slug": slug,
                "source_m3u8": preview_url
            }
    except Exception as e:
        print(f"[Engine] yt-dlp metadata error: {e}")

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
    Clips a Kick or Twitch VOD or stream segment between start_time and end_time.
    Uses native stream-copy keyframe slicing via yt-dlp.
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

    is_twitch = "twitch.tv" in url.lower() or "twitch" in url.lower()
    prefix = "twitch" if is_twitch else "kick"
    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_filename = f"{prefix}_clip_{timestamp_str}.mp4"
    final_output = out_path / out_filename

    # If URL is a Kick webpage, resolve it to direct m3u8 source.
    # NEVER overwrite Twitch URLs because yt-dlp native Twitch extractor handles authentication!
    target_media = url.strip()
    if not is_twitch and not target_media.endswith(".m3u8"):
        resolved_info = get_kick_info(target_media)
        if resolved_info.get("success") and resolved_info.get("source_m3u8"):
            target_media = resolved_info["source_m3u8"]

    # Format quality selector for yt-dlp
    q_lower = quality.lower().strip()
    if q_lower == "best":
        format_selector = "best"
    elif "1080" in q_lower:
        format_selector = "best[height<=1080]/best"
    elif "720" in q_lower:
        format_selector = "best[height<=720]/best"
    elif "480" in q_lower:
        format_selector = "best[height<=480]/best"
    elif "360" in q_lower:
        format_selector = "best[height<=360]/best"
    elif "160" in q_lower:
        format_selector = "best[height<=160]/best"
    else:
        format_selector = f"{quality}/best"

    section_arg = f"*{seconds_to_time(start_sec)}-{seconds_to_time(end_sec)}"

    # Note: We omit --force-keyframes-at-cuts to allow blazing fast stream-copy without 100% CPU re-encoding!
    cmd = [
        "yt-dlp",
        "--download-sections", section_arg,
        "-f", format_selector,
        "-o", str(final_output),
        "--no-playlist",
        "--merge-output-format", "mp4",
        "--newline",
        target_media
    ]

    if progress_callback:
        progress_callback(8.0, "Connecting to stream server...")

    last_pct = 8.0

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        buffer = ""
        while True:
            chunk = proc.stdout.read(64)
            if not chunk and proc.poll() is not None:
                break
            buffer += chunk
            while '\r' in buffer or '\n' in buffer:
                m = re.search(r'[\r\n]', buffer)
                line_str = buffer[:m.start()].strip()
                buffer = buffer[m.end():]
                if not line_str:
                    continue

                # Stream downloading status
                if "[info]" in line_str and "Downloading 1 time ranges" in line_str:
                    if last_pct < 15.0:
                        last_pct = 15.0
                        if progress_callback:
                            progress_callback(15.0, "Locating video stream segments...")

                # FFmpeg time progress (e.g. time=00:00:11.93)
                m_time = re.search(r'time=(\d+):(\d+):([\d\.]+)', line_str)
                if m_time:
                    sec = int(m_time.group(1)) * 3600 + int(m_time.group(2)) * 60 + float(m_time.group(3))
                    if clip_duration > 0:
                        ratio = min(1.0, max(0.0, sec / clip_duration))
                        calc_pct = 15.0 + (ratio * 80.0)
                        if calc_pct > last_pct:
                            last_pct = round(calc_pct, 1)
                            if progress_callback:
                                progress_callback(last_pct, f"Slicing clip: {last_pct}% ({seconds_to_time(int(sec))}/{seconds_to_time(clip_duration)})")
                    continue

                # yt-dlp download percentage
                m_dl = re.search(r'\[download\]\s+([\d\.]+)%', line_str)
                if m_dl:
                    try:
                        val = float(m_dl.group(1))
                        ratio = min(1.0, max(0.0, val / 100.0))
                        calc_pct = 15.0 + (ratio * 80.0)
                        if calc_pct > last_pct:
                            last_pct = round(calc_pct, 1)
                            if progress_callback:
                                progress_callback(last_pct, f"Downloading stream segments: {last_pct}%")
                    except ValueError:
                        pass
                    continue

                # Container packaging
                if "[FixupM3u8]" in line_str or "Fixing" in line_str or "Merging" in line_str:
                    if last_pct < 96.0:
                        last_pct = 96.0
                        if progress_callback:
                            progress_callback(96.0, "Packaging MP4 clip...")

        proc.wait(timeout=600)

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
            return False, "Trimming failed: video segment could not be generated. Please try a shorter segment or different resolution.", {}

    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
        return False, "Process timed out. For longer clips, please try a segment under 15 minutes.", {}
    except Exception as e:
        return False, f"Unexpected error: {str(e)}", {}
