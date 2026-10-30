---
title: KickCuttr - Online Kick Video Clipper
emoji: ⚡
colorFrom: green
colorTo: gray
sdk: gradio
app_file: app.py
pinned: false
license: mit
---

# ⚡ KickCuttr - Online Kick.com Video Clipper

**KickCuttr** is a fast, lightweight online video clipper specifically built for Kick.com streams, VODs, and clips.

## 🌟 Features
- **Fast Segment Clipping**: Trim any Kick VOD or clip by setting Start and End times.
- **Multiple Qualities**: 1080p, 720p, 480p, or Best available.
- **No Heavy Downloads**: Slices stream segments directly via `yt-dlp` and `ffmpeg`.
- **In-Browser Video Preview**: Play your clipped video immediately in the browser before downloading.
- **Free Hugging Face Hosting**: Runs on free CPU hardware without requiring a custom domain.

## 🚀 How to Run Locally

```bash
pip install -r requirements.txt
python app.py
```
Open `http://localhost:7860` in your web browser.
