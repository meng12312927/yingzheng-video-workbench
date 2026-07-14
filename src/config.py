"""
项目配置中心 — 所有可配置的参数都在这里
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# 项目路径
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"

# OpenAI API 配置
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not OPENAI_API_KEY:
    print("[WARNING] OPENAI_API_KEY not found. Set it in .env file.")

# Whisper 配置 (Mac Apple Silicon: CPU + int8)
WHISPER_MODEL_SIZE = "large-v3"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"

# FFmpeg 配置 (Mac: videotoolbox, Win: h264_nvenc)
VIDEO_ENCODER = "h264_videotoolbox"
SUBTITLE_FONT_SIZE = 18
SUBTITLE_FONT_COLOR = "white"

# 确保目录存在
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
