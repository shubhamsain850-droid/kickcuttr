"""
FastAPI APIRouter for YouTube Live Stream Earning & Chat Analytics
Can be plugged into ANY existing FastAPI app (e.g. kickcuttr main.py) with zero friction!
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File
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
    get_channel_streams,
    get_cookie_arg
)

router = APIRouter()

# ─────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────

class AnalyzeStreamRequest(BaseModel):
    url: str

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

@router.post("/analyze-stream")
def analyze_stream_endpoint(req: AnalyzeStreamRequest):
    """
    Downloads live chat replay for the given YouTube video ID/URL and extracts:
    - Superchats & Super Stickers
    - Memberships (Dynamic Custom Tiers & Milestones)
    - YouTube Jewels
    - Total Gross & Net Earnings
    """
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="YouTube URL or Video ID is required.")

    vid = extract_video_id(url)
    if not vid:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL or Video ID.")

    # 1. Fetch real video title with cookies
    stream_title = f"Live Stream [{vid}]"
    title_cmd = ["yt-dlp"] + get_cookie_arg() + [
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

    # 2. Download live chat replay json in isolated temp dir
    with tempfile.TemporaryDirectory() as tmp_dir:
        chat_output_template = os.path.join(tmp_dir, f"{vid}.%(ext)s")
        cmd = ["yt-dlp"] + get_cookie_arg() + [
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
                "message": f"Downloading chat for '{stream_title}' took longer than 3 minutes. Please use Option D (upload JSON directly) for instant audit.",
                "video_id": vid
            }

        found_files = list(Path(tmp_dir).glob(f"*{vid}*.json"))
        
        if not found_files:
            return {
                "success": False,
                "error": "NO_CHAT_AVAILABLE",
                "title": stream_title,
                "message": f"Live chat replay was not found or is disabled for '{stream_title}'.",
                "video_id": vid
            }

        chat_file = found_files[0]
        with open(chat_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        parsed_data = parse_chat_json_lines(lines, video_id=vid)
        return {
            "success": True,
            "video_id": vid,
            "title": stream_title,
            "data": parsed_data
        }

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
