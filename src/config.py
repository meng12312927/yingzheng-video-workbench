"""
项目配置中心 — 所有可配置的参数都在这里
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 项目路径
PROJECT_ROOT = Path(__file__).parent.parent

load_dotenv()
# .env.deepseek 是本机私有配置，优先级高于通用 .env，且不会提交到 Git。
load_dotenv(PROJECT_ROOT / ".env.deepseek", override=True)

DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"

# OpenAI 兼容 LLM 配置；默认仍可使用 OpenAI，也可切换 DeepSeek。
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
LLM_API_KEY = os.getenv("LLM_API_KEY", OPENAI_API_KEY)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", OPENAI_MODEL)
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))

# Embedding 可与聊天模型使用不同服务；向量索引仍在本地建立。
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", OPENAI_API_KEY)
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

if not LLM_API_KEY:
    print("[WARNING] No LLM API key found. Set LLM_API_KEY in .env or .env.deepseek.")

# macOS 默认配置。Faster-Whisper 在 macOS 上稳定使用 CPU + int8；
# 需要更高识别质量时可在 .env 中将模型改为 large-v3。
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

# macOS 使用 VideoToolbox 硬件编码；若本机 FFmpeg 不支持，可设为 libx264。
VIDEO_ENCODER = os.getenv("VIDEO_ENCODER", "h264_videotoolbox")
SUBTITLE_FONT_SIZE = 18
SUBTITLE_FONT_COLOR = "white"
# 留空时让 Gradio 自动在 7860–7959 中寻找可用端口；需要固定端口再在 .env 中设置。
_gradio_port = os.getenv("GRADIO_SERVER_PORT", "").strip()
GRADIO_SERVER_PORT = int(_gradio_port) if _gradio_port else None

# 确保目录存在
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
