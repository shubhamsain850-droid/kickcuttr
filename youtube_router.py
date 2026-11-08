"""
FastAPI APIRouter for YouTube Live Stream Earning & Chat Analytics
Can be plugged into ANY existing FastAPI app (e.g. kickcuttr main.py) with zero friction!
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Depends, Response
from pydantic import BaseModel
from typing import Optional, List
import subprocess
import tempfile
import json
import os
import re
from pathlib import Path

from youtube_engine import (
    extract_video_id,
    parse_chat_json_lines,
    parse_all_chat_messages,
    get_channel_streams,
    get_cookie_arg,
    fetch_innertube_stream_data,
    fetch_video_comments
)

def anti_cache_headers(response: Response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

router = APIRouter(dependencies=[Depends(anti_cache_headers)])

# ─────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────

class AnalyzeStreamRequest(BaseModel):
    url: str

class FetchChatRequest(BaseModel):
    url: str

class FetchCommentsRequest(BaseModel):
    url: str
    max_comments: Optional[int] = 300

class ChannelStreamsRequest(BaseModel):
    channel_url: str
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    max_results: Optional[int] = 50

# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@router.get("/health")
def yt_analytics_health():
    return {
        "status": "healthy",
        "service": "YouTube Stream Analytics Engine",
        "version": "1.2.0"
    }

@router.post("/channel-streams")
def fetch_channel_streams_endpoint(req: ChannelStreamsRequest):
    """
    Fetch all live streams for a channel, filtered by optional date range.
    """
    if not req.channel_url.strip():
        raise HTTPException(status_code=400, detail="Channel URL or handle is required.")
    
    streams = get_channel_streams(
        channel_url=req.channel_url,
        from_date=req.from_date,
        to_date=req.to_date,
        max_results=req.max_results or 50
    )
    return {
        "success": True,
        "channel": req.channel_url,
        "count": len(streams),
        "streams": streams
    }

def frozen_ytdlp_analyze_stream(vid: str, stream_title: Optional[str] = None, include_all_chat: bool = False) -> dict:
    """
    [FROZEN ENGINE - PRESERVED AS PERMANENT AUTOMATIC FALLBACK]
    Downloads live chat replay via yt-dlp subprocess in isolated temp directory.
    Invoked automatically if direct InnerTube stream is blocked or unavailable.
    """
    if not stream_title:
        stream_title = f"Live Stream [{vid}]"
        title_cmd = ["yt-dlp"] + get_cookie_arg() + [
            "--ignore-no-formats-error",
            "--skip-download",
            "--print", "%(title)s",
            f"https://www.youtube.com/watch?v={vid}"
        ]
        try:
            t_res = subprocess.run(title_cmd, capture_output=True, text=True, timeout=15)
            if t_res.returncode == 0 and t_res.stdout.strip():
                stream_title = t_res.stdout.strip()
        except Exception:
            pass

    # Download live chat replay json in isolated temp dir
    with tempfile.TemporaryDirectory() as tmp_dir:
        chat_output_template = os.path.join(tmp_dir, f"{vid}.%(ext)s")
        cmd = ["yt-dlp"] + get_cookie_arg() + [
            "--ignore-no-formats-error",
            "--skip-download",
            "--write-sub",
            "--sub-langs", "live_chat",
            "-o", chat_output_template,
            f"https://www.youtube.com/watch?v={vid}"
        ]

        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "TIMEOUT",
                "title": stream_title,
                "message": f"Downloading chat for '{stream_title}' took longer than 3 minutes. Please check your connection or try again.",
                "video_id": vid,
                "engine": "frozen_ytdlp_fallback"
            }

        found_files = list(Path(tmp_dir).glob(f"*{vid}*.json"))
        
        if not found_files:
            return {
                "success": False,
                "error": "NO_CHAT_AVAILABLE",
                "title": stream_title,
                "message": f"Live chat replay was not found or is disabled for '{stream_title}'.",
                "video_id": vid,
                "engine": "frozen_ytdlp_fallback"
            }

        chat_file = found_files[0]
        with open(chat_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        if include_all_chat:
            parsed_data = parse_all_chat_messages(lines, video_id=vid)
        else:
            parsed_data = parse_chat_json_lines(lines, video_id=vid)

        return {
            "success": True,
            "video_id": vid,
            "title": stream_title,
            "data": parsed_data,
            "engine": "frozen_ytdlp_fallback"
        }


@router.post("/analyze-stream")
def analyze_stream_endpoint(req: AnalyzeStreamRequest):
    """
    Dual-Engine YouTube Live Stream Analyzer:
    1. Primary Engine: Ultra-Fast Direct InnerTube Replay Fetcher (100% in-memory, zero disk, 10-15s)
    2. Frozen Fallback Engine: yt-dlp Subprocess (100% preserved as automatic fallback)
    """
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="YouTube URL or Video ID is required.")

    vid = extract_video_id(url)
    if not vid:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL or Video ID.")

    # 1. Primary Engine: Ultra-Fast Direct InnerTube Streaming (100% in-memory, zero disk footprint)
    try:
        innertube_res = fetch_innertube_stream_data(vid, include_all_chat=False)
        if innertube_res and innertube_res.get("success"):
            return innertube_res
    except Exception as e:
        print(f"[InnerTube Engine Error] Video {vid}: {e}. Switching to frozen yt-dlp fallback...")

    # 2. Frozen Fallback Engine: yt-dlp Subprocess (100% Preserved)
    print(f"[Fallback] Running frozen yt-dlp engine for video {vid}...")
    return frozen_ytdlp_analyze_stream(vid, include_all_chat=False)


@router.post("/fetch-chat")
def fetch_chat_endpoint(req: FetchChatRequest):
    """
    Downloads & extracts ALL live chat messages (normal chat, superchats, stickers, memberships, jewels)
    with usernames, timestamps, author badges, event highlights, and top chatters leaderboard.
    """
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="YouTube URL or Video ID is required.")

    vid = extract_video_id(url)
    if not vid:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL or Video ID.")

    # 1. Primary Engine: Ultra-Fast Direct InnerTube Streaming (100% in-memory, zero disk)
    try:
        res = fetch_innertube_stream_data(vid, include_all_chat=True)
        if res and res.get("success"):
            return res
    except Exception as e:
        print(f"[Fetch-Chat InnerTube Error] Video {vid}: {e}. Switching to frozen yt-dlp fallback...")

    # 2. Frozen Fallback Engine
    return frozen_ytdlp_analyze_stream(vid, include_all_chat=True)


@router.post("/fetch-comments")
def fetch_comments_endpoint(req: FetchCommentsRequest):
    """
    Directly extracts comments from an uploaded or saved YouTube video.
    Extracts comment text, author name, avatar, relative date, likes, and reply counts.
    """
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="YouTube URL or Video ID is required.")

    vid = extract_video_id(url)
    if not vid:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL or Video ID.")

    max_c = min(req.max_comments or 300, 1000)
    return fetch_video_comments(vid, max_comments=max_c)


@router.post("/upload-json")
async def analyze_uploaded_json_endpoint(file: UploadFile = File(...)):
    """
    Upload a local .live_chat.json file directly to audit earnings instantly.
    """
    try:
        content = await file.read()
        lines = content.decode("utf-8", errors="ignore").splitlines()
        clean_name = file.filename.replace(".live_chat.json", "").replace(".json", "")
        parsed_data = parse_chat_json_lines(lines, video_id=clean_name)
        return {
            "success": True,
            "title": clean_name,
            "filename": file.filename,
            "data": parsed_data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse uploaded JSON file: {str(e)}")


@router.post("/upload-chat-json")
async def analyze_uploaded_chat_json_endpoint(file: UploadFile = File(...)):
    """
    Upload a local .live_chat.json file to parse ALL chats and events instantly.
    """
    try:
        content = await file.read()
        lines = content.decode("utf-8", errors="ignore").splitlines()
        clean_name = file.filename.replace(".live_chat.json", "").replace(".json", "")
        parsed_data = parse_all_chat_messages(lines, video_id=clean_name)
        return {
            "success": True,
            "title": clean_name,
            "filename": file.filename,
            "data": parsed_data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse uploaded JSON file: {str(e)}")
