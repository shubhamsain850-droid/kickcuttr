"""
KickCuttr - Online Kick.com Video Clipper
A fast, lightweight online clipper for Kick VODs, Clips, and Streams.
Designed for zero-config deployment on Hugging Face Spaces (Free CPU tier, no custom domain needed).
"""

import os
import sys
from pathlib import Path
import gradio as gr

from kick_engine import (
    get_kick_info,
    clip_kick_video,
    time_to_seconds,
    seconds_to_time
)

OUTPUT_DIR = Path("/tmp/kickcuttr" if sys.platform != "win32" else os.path.expanduser("~/.kickcuttr_tmp"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CUSTOM_CSS = """
body {
    background-color: #0b0e14 !important;
}
.gradio-container {
    max-width: 960px !important;
    margin: auto !important;
}
.kick-header {
    text-align: center;
    padding: 1.8rem 1rem;
    background: linear-gradient(135deg, #0e121a 0%, #151b26 100%);
    border: 1px solid #1f293d;
    border-radius: 14px;
    margin-bottom: 1.5rem;
}
.kick-title {
    font-size: 2.3rem;
    font-weight: 800;
    color: #ffffff;
    margin-bottom: 0.3rem;
    letter-spacing: -0.5px;
}
.kick-title span {
    color: #53fc18;
}
.kick-subtitle {
    font-size: 1.05rem;
    color: #94a3b8;
}
.kick-btn-primary {
    background-color: #53fc18 !important;
    color: #000000 !important;
    font-weight: 700 !important;
    border: none !important;
    border-radius: 8px !important;
    font-size: 1rem !important;
}
.kick-btn-primary:hover {
    background-color: #44cf14 !important;
}
.info-card {
    background: #131a26;
    border: 1px solid #233045;
    border-radius: 10px;
    padding: 15px;
    margin: 10px 0;
}
"""

def fetch_video_details(url: str):
    """Fetches video info and fills in default start/end times."""
    if not url or not url.strip():
        return None, "⚠️ Please enter a Kick video, VOD, or clip URL.", "00:00:00", "00:01:00"

    info = get_kick_info(url.strip())
    if not info.get("success"):
        return None, f"❌ Error: {info.get('error', 'Could not fetch video info.')}", "00:00:00", "00:01:00"

    title = info.get("title", "Kick Video")
    uploader = info.get("uploader", "Creator")
    stream_id = info.get("stream_id", "N/A")
    duration = info.get("duration", 0)
    duration_str = info.get("duration_str", "Unknown")
    thumb = info.get("thumbnail")
    is_live = info.get("is_live", False)

    status_tag = "🔴 LIVE STREAM" if is_live else "📼 RECORDED VOD / CLIP"
    status_bg = "#ef4444" if is_live else "#10b981"

    card_html = f"""
    <div class="info-card">
        <div style="display:flex; gap:16px; align-items:center; flex-wrap:wrap;">
            {'<img src="' + thumb + '" style="width:170px; height:96px; object-fit:cover; border-radius:8px; border:1px solid #334155;"/>' if thumb else ''}
            <div style="flex:1; min-width:240px;">
                <span style="background:{status_bg}; color:#fff; font-size:11px; font-weight:bold; padding:3px 8px; border-radius:4px;">{status_tag}</span>
                <h3 style="color:#ffffff; margin:8px 0 5px 0; font-size:1.15rem; line-height:1.3;">{title}</h3>
                <p style="color:#94a3b8; margin:0; font-size:0.9rem;">
                    <strong>Channel:</strong> <span style="color:#53fc18;">{uploader}</span> &nbsp;|&nbsp;
                    <strong>Stream/Video ID:</strong> <code>{stream_id}</code> &nbsp;|&nbsp;
                    <strong>Duration:</strong> {duration_str}
                </p>
            </div>
        </div>
    </div>
    """

    default_start = "00:00:00"
    if duration > 60:
        default_end = "00:01:00"
    elif duration > 0:
        default_end = seconds_to_time(duration)
    else:
        default_end = "00:01:00"

    return card_html, "✅ Video found! Enter your Start & End timestamps, then click 'Cut Clip Now'.", default_start, default_end


def cut_clip(url: str, start_time: str, end_time: str, quality: str):
    """Processes clip cutting and produces the final MP4 file."""
    if not url or not url.strip():
        return None, "⚠️ Please enter a Kick URL first.", None

    s_sec = time_to_seconds(start_time)
    e_sec = time_to_seconds(end_time)

    if e_sec <= s_sec:
        return None, "❌ End Time must be greater than Start Time.", None

    clip_len = e_sec - s_sec
    if clip_len > 7200:
        return None, "❌ Max clip duration is 2 hours.", None

    success, msg, meta = clip_kick_video(
        url=url.strip(),
        start_time=start_time,
        end_time=end_time,
        quality=quality,
        output_dir=str(OUTPUT_DIR)
    )

    if not success:
        return None, f"❌ Failed: {msg}", None

    clip_file = meta.get("file_path")
    size_mb = meta.get("size_mb", 0)
    dur_str = meta.get("duration_str", "")

    status = f"🎉 **Clip Ready!** Length: `{dur_str}` | Size: `{size_mb} MB`"
    return clip_file, status, clip_file


# Build Gradio UI
with gr.Blocks(title="KickCuttr - Online Kick Video Clipper", css=CUSTOM_CSS, theme=gr.themes.Base()) as demo:
    gr.HTML("""
    <div class="kick-header">
        <div class="kick-title">⚡ Kick<span>Cuttr</span></div>
        <div class="kick-subtitle">Fast Online Video Clipper for Kick.com Streams, VODs & Clips</div>
    </div>
    """)

    with gr.Tabs():
        # TAB 1: CLIPPER
        with gr.Tab("🎬 Video Clipper"):
            with gr.Row():
                with gr.Column(scale=4):
                    url_in = gr.Textbox(
                        label="Kick Video / VOD / Clip URL",
                        placeholder="https://kick.com/video/UUID or https://kick.com/xqc?clip=...",
                        lines=1
                    )
                with gr.Column(scale=1):
                    fetch_btn = gr.Button("🔍 Fetch Info", variant="primary")

            # Video Info Card
            info_card_out = gr.HTML()
            status_hint = gr.Markdown()

            # Trimming Options
            with gr.Row():
                start_in = gr.Textbox(label="Start Time (HH:MM:SS or Seconds)", value="00:00:00")
                end_in = gr.Textbox(label="End Time (HH:MM:SS or Seconds)", value="00:01:00")
                quality_in = gr.Dropdown(
                    label="Quality",
                    choices=["1080p", "720p (Recommended)", "480p", "Best Available"],
                    value="720p (Recommended)"
                )

            cut_btn = gr.Button("✂️ Cut Clip Now", variant="primary", elem_classes=["kick-btn-primary"])

            clip_status = gr.Markdown()
            with gr.Row():
                player_out = gr.Video(label="Clip Preview", interactive=False)
                file_out = gr.File(label="📥 Download Clipped MP4")

            # Bindings
            fetch_btn.click(
                fn=fetch_video_details,
                inputs=[url_in],
                outputs=[info_card_out, status_hint, start_in, end_in]
            )

            cut_btn.click(
                fn=cut_clip,
                inputs=[url_in, start_in, end_in, quality_in],
                outputs=[player_out, clip_status, file_out]
            )

        # TAB 2: DEPLOYMENT GUIDE
        with gr.Tab("🚀 Hugging Face Deployment Guide"):
            gr.Markdown("""
            ## 🌐 Hugging Face Spaces Deployment Guide (Free & No Custom Domain Needed)
            
            Is project ko aap bina kisi custom domain ke **Hugging Face Spaces** par bilkul free host kar sakte ho:

            ### Step 1: Hugging Face par Space Create karein
            1. [Hugging Face Spaces](https://huggingface.co/spaces) par jayein aur **"Create new Space"** par click karein.
            2. **Space name:** `kickcuttr` (ya jo naam aap chaho)
            3. **Select the Space SDK:** **Gradio** select karein.
            4. **Space hardware:** **Free (CPU basic - 2 vCPU, 16GB RAM)** select karein.
            5. **Visibility:** Public select karke **"Create Space"** dabayein.

            ---

            ### Step 2: Files Upload karein
            Aapko bas ye files upload karni hain:
            - `app.py`
            - `kick_engine.py`
            - `requirements.txt`
            - `packages.txt`
            - `README.md`

            ---

            ### Step 3: Tool Live!
            1-2 minute ke andar aapka Hugging Face tool ready ho jayega:
            `https://<your-username>-kickcuttr.hf.space`
            Isko bina domain ke direct browser me use kar sakte hain!
            """)

from main import app as fastapi_app

if __name__ == "__main__":
    res = demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        prevent_thread_lock=True,
        allowed_paths=[str(OUTPUT_DIR), "/tmp"]
    )
    launched_app = res[0]
    launched_app.include_router(fastapi_app.router)
    demo.block_thread()
