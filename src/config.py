"""
===========================================================================
config.py — 项目配置中心
===========================================================================
所有可配置的参数都在这里，方便统一管理。

你怎么理解这个文件？
- 就像手机的"设置"页面，所有开关都在一个地方
- 其他代码从这里读取配置，而不是把 API Key 硬编码在各处

Python 知识点：
- os.getenv() 从环境变量（.env 文件）读取值
- Path 对象是跨平台的路径处理方式（Windows 和 Mac 都能用）
===========================================================================
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量（API Key 等）
load_dotenv()

# ============================================
# 项目路径
# ============================================
# Path(__file__)     = 当前文件路径（src/config.py）
# .parent            = 上一级目录（src/）
# .parent            = 再上一级（项目根目录）
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"


# ============================================
# OpenAI API 配置
# ============================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# 如果没有设置 API Key，启动时会提醒
if not OPENAI_API_KEY:
    print("[WARNING] OPENAI_API_KEY not found. Please set it in .env file.")
    print("           Copy .env.example to .env and fill in your API key.")


# ============================================
# Whisper 语音识别配置
# ============================================
# 模型大小选择：
#   tiny      (39MB)   - 最快，准确率最低
#   base      (74MB)   - 快速
#   small     (244MB)  - 平衡
#   medium    (769MB)  - 较准确
#   large-v3  (1550MB) - 最准确，RTX 4060 完全能跑
WHISPER_MODEL_SIZE = "large-v3"

# device = "cuda" → 使用 GPU 加速（你有 RTX 4060）
# device = "cpu"  → 纯 CPU（慢 10-50 倍）
WHISPER_DEVICE = "cuda"

# compute_type: 推理精度
#   float16  - 半精度，速度快（推荐 GPU 使用）
#   int8     - 量化，省显存但稍慢
WHISPER_COMPUTE_TYPE = "float16"


# ============================================
# 视频处理配置
# ============================================
# 输出视频编码器
#   h264_nvenc  - NVIDIA GPU 硬件编码（快！）
#   libx264     - CPU 软件编码（兼容性好）
VIDEO_ENCODER = "h264_nvenc"

# 字幕样式
SUBTITLE_FONT_SIZE = 18
SUBTITLE_FONT_COLOR = "white"


# ============================================
# 确保必要目录存在
# ============================================
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
