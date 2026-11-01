"""
KickCutter Backend API (FastAPI)
Provides endpoints for:
- POST /info: Get video title, thumbnail, duration, streamer name, resolutions, preview URL.
- POST /clip: Start background video clipping job with real-time percentage tracking.
- GET /status/{job_id}: Check job status, progress percentage (0-100%), download URL.
- GET /download/{filename}: Download trimmed MP4 video file.
- GET /health: API health check.
"""

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uuid
import datetime
from pathlib import Path
import os
import sys

# Import core clipping engine
from kick_engine import (
    get_kick_info,
    clip_kick_video,
    time_to_seconds,
    seconds_to_time
)

app = FastAPI(title="KickCutter API", description="Backend API for Kick.com Video Trimming")

# Enable CORS for Cloudflare Pages frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CLIPS_DIR = Path("/tmp/kickcuttr" if sys.platform != "win32" else os.path.expanduser("~/.kickcuttr_tmp"))
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

# In-memory jobs tracking
jobs = {}

class InfoRequest(BaseModel):
    url: str

class ClipRequest(BaseModel):
    url: str
    start: str
    end: str
    quality: str = "720p"

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok", "service": "KickCutter API", "version": "1.1.0"}

@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "healthy", "timestamp": datetime.datetime.now().isoformat()}

@app.post("/info")
def get_info(req: InfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Kick URL is required.")
    
    info = get_kick_info(url)
    if not info.get("success"):
        raise HTTPException(status_code=400, detail=info.get("error", "Failed to fetch video information."))
    
    return {
        "success": True,
        "title": info.get("title", "Kick Video"),
        "uploader": info.get("uploader", "Creator"),
        "stream_id": info.get("stream_id", "N/A"),
        "duration": info.get("duration", 0),
        "duration_str": info.get("duration_str", "00:00"),
        "thumbnail": info.get("thumbnail"),
        "is_live": info.get("is_live", False),
        "preview_url": info.get("preview_url", ""),
        "available_resolutions": info.get("available_resolutions", []),
        "slug": info.get("slug", ""),
        "webpage_url": info.get("webpage_url", url)
    }

def run_clip_job(job_id: str, url: str, start: str, end: str, quality: str):
    jobs[job_id]["status"] = "processing"
    jobs[job_id]["progress"] = 5.0
    jobs[job_id]["message"] = "Starting stream slicer..."

    def on_progress(pct: float, message: str):
        if job_id in jobs:
            jobs[job_id]["progress"] = round(pct, 1)
            jobs[job_id]["message"] = message

    try:
        success, msg, meta = clip_kick_video(
            url=url,
            start_time=start,
            end_time=end,
            quality=quality,
            output_dir=str(CLIPS_DIR),
            progress_callback=on_progress
        )
        if success:
            filename = meta.get("filename")
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["progress"] = 100.0
            jobs[job_id]["message"] = "100% Complete! Ready for download."
            jobs[job_id]["file_path"] = meta.get("file_path")
            jobs[job_id]["filename"] = filename
            jobs[job_id]["size_mb"] = meta.get("size_mb")
            jobs[job_id]["duration_str"] = meta.get("duration_str")
            jobs[job_id]["download_url"] = f"/download/{filename}"
        else:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = msg
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)

@app.post("/clip")
def start_clip(req: ClipRequest, bg_tasks: BackgroundTasks):
    url = req.url.strip()
    start = req.start.strip()
    end = req.end.strip()
    quality = req.quality.strip()

    if not url:
        raise HTTPException(status_code=400, detail="Kick URL is required.")
    
    s_sec = time_to_seconds(start)
    e_sec = time_to_seconds(end)
    if e_sec <= s_sec:
        raise HTTPException(status_code=400, detail="End time must be greater than Start time.")
    if (e_sec - s_sec) > 7200:
        raise HTTPException(status_code=400, detail="Max clip duration is 2 hours.")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "progress": 5.0,
        "message": "Queued for processing...",
        "created_at": datetime.datetime.now().isoformat()
    }

    bg_tasks.add_task(run_clip_job, job_id, url, start, end, quality)
    return {"success": True, "job_id": job_id, "status": "pending"}

@app.get("/status/{job_id}")
def get_job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job ID not found.")
    return jobs[job_id]

@app.get("/download/{filename}")
def download_clip(filename: str):
    safe_name = os.path.basename(filename)
    file_path = CLIPS_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Requested clip file not found.")
    return FileResponse(
        path=str(file_path),
        media_type="video/mp4",
        filename=safe_name,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'}
    )
