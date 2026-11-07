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
from pathlib import Path

from youtube_engine import (
    extract_video_id,
    parse_chat_json_lines,
    get_channel_streams
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
        "version": "1.0.0"
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
    - Superchats
    - Memberships
    - Jewels
    - Total Gross & Net Earnings
    """
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="YouTube URL or Video ID is required.")

    vid = extract_video_id(url)
    if not vid:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL or Video ID.")

    # Create isolated temp folder for downloading chat json
    with tempfile.TemporaryDirectory() as tmp_dir:
        chat_output_template = os.path.join(tmp_dir, f"{vid}.%(ext)s")
        
        # yt-dlp command to fetch only live_chat subtitle/json without downloading video
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-sub",
            "--sub-langs", "live_chat",
            "--sub-format", "json",
            "-o", chat_output_template,
            f"https://www.youtube.com/watch?v={vid}"
        ]

        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Timeout while downloading live chat from YouTube.")

        # Locate downloaded chat file (.json or .live_chat.json)
        found_files = list(Path(tmp_dir).glob(f"*{vid}*.json"))
        
        if not found_files:
            return {
                "success": False,
                "error": "NO_CHAT_AVAILABLE",
                "message": "Live chat replay is either disabled, private, or unavailable for this stream.",
                "video_id": vid
            }

        chat_file = found_files[0]
        with open(chat_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        parsed_data = parse_chat_json_lines(lines, video_id=vid)
        return {
            "success": True,
            "video_id": vid,
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
        parsed_data = parse_chat_json_lines(lines, video_id=file.filename)
        return {
            "success": True,
            "filename": file.filename,
            "data": parsed_data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse uploaded JSON file: {str(e)}")
