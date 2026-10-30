import spaces
import gradio as gr
from main import app as fastapi_app

# ZeroGPU binding for Hugging Face free tier
@spaces.GPU
def predict(prompt: str = "") -> str:
    return "⚡ KickCutter API Server is Active and Running with ZeroGPU!"

demo = gr.Interface(
    fn=predict,
    inputs=gr.Textbox(label="Status Check", value="ping"),
    outputs=gr.Textbox(label="Response"),
    title="⚡ KickCutter API Server",
    description="Backend API for [kickcuttr.pages.dev](https://kickcuttr.pages.dev)"
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
