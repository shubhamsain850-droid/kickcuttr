import spaces
import gradio as gr
import subprocess, os, sys, shutil, urllib.request, zipfile
from pathlib import Path

# 1. Deno bootstrap for yt-dlp EJS challenge solver (Linux only)
def ensure_deno():
    deno_dir = Path.home() / ".local" / "bin"
    deno_exe = deno_dir / "deno"
    if deno_exe.exists():
        try:
            ver = subprocess.run([str(deno_exe), "--version"], capture_output=True, text=True, timeout=5).stdout
            if "2." in ver:
                os.environ["PATH"] = f"{deno_dir}:{os.environ.get('PATH', '')}"
                return str(deno_exe)
        except Exception:
            pass
        try:
            deno_exe.unlink()
        except Exception:
            pass

    try:
        print("[Bootstrap] Downloading Deno v2.9.8 for yt-dlp EJS solver...")
        deno_dir.mkdir(parents=True, exist_ok=True)
        zip_url = "https://github.com/denoland/deno/releases/download/v2.9.8/deno-x86_64-unknown-linux-gnu.zip"
        zip_dest = Path("/tmp/deno.zip")
        urllib.request.urlretrieve(zip_url, zip_dest)
        with zipfile.ZipFile(zip_dest, "r") as zip_ref:
            zip_ref.extractall(deno_dir)
        deno_exe.chmod(0o755)
        os.environ["PATH"] = f"{deno_dir}:{os.environ.get('PATH', '')}"
        print(f"[Bootstrap] Deno 2.9.8 installed at {deno_exe}")
        return str(deno_exe)
    except Exception as e:
        print(f"[Bootstrap] Deno setup failed: {e}")
        return None

if os.name != "nt":
    ensure_deno()

from main import app as fastapi_app

# ZeroGPU binding for Hugging Face free tier
@spaces.GPU
def predict(prompt: str = "") -> str:
    return "⚡ KickCutter & YouTube Stream Analytics API Server Active and Running with ZeroGPU!"

demo = gr.Interface(
    fn=predict,
    inputs=gr.Textbox(label="Status Check", value="ping"),
    outputs=gr.Textbox(label="Response"),
    title="⚡ KickCutter API Server",
    description="Backend API for [kickcutter.pages.dev](https://kickcutter.pages.dev) and [yt-stream-analytics.pages.dev](https://yt-stream-analytics.pages.dev)"
)

if __name__ == "__main__":
    res = demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        prevent_thread_lock=True,
        allowed_paths=["/tmp/kickcuttr", "/tmp"]
    )
    launched_app = res[0]
    launched_app.include_router(fastapi_app.router)
    demo.block_thread()
