# -*- coding: utf-8 -*-
from faster_whisper import WhisperModel
from pathlib import Path
from src.config import WHISPER_COMPUTE_TYPE, WHISPER_DEVICE, WHISPER_MODEL_SIZE


class WhisperTool:
    def __init__(
        self,
        model_size: str = WHISPER_MODEL_SIZE,
        device: str = WHISPER_DEVICE,
        compute_type: str = WHISPER_COMPUTE_TYPE,
    ):
       
        print(f"[Whisper] 正在加载模型 '{model_size}'，设备: {device}...")
        print("[Whisper] （首次运行会下载约 1.5GB 模型文件，请耐心等待）")

        # WhisperModel 是 Faster-Whisper 提供的核心类
        # 它会自动从 HuggingFace 下载模型到本地缓存
        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
        )
        self.device = device
        print("[Whisper] 模型加载完成！")

    def transcribe(self, video_path: str, language: str = "zh") -> list[dict]:
        """
        对视频进行语音转文字

        参数：
          video_path: 视频文件路径（mp4, avi, mov, mkv 都支持）
          language: 语言代码，"zh"=中文，"en"=英文，None=自动检测

        返回值：
          一个列表，每个元素是一个字典：
          [
            {"start": 0.0, "end": 3.5, "text": "各位老师同学们大家好"},
            {"start": 3.5, "end": 7.2, "text": "欢迎来到本届运动会"},
            ...
          ]

        Python 知识点：
          这个函数的返回值类型是 list[dict]，意思是：
          - list = 列表，可以放多个东西
          - dict = 字典，用 {key: value} 存储，"start" 是 key，0.0 是 value
        """
        print(f"[Whisper] 开始转录: {video_path}")
        print(f"[Whisper] 语言: {language}")

        # model.transcribe() 返回两个值：
        #   segments: 转录片段（我们要的）
        #   info: 元信息（语言检测结果等，暂时不用）
        # 用 _ 来接收"不关心的返回值"是 Python 的惯例写法
        segments, _ = self.model.transcribe(
            video_path,
            language=language,
            # beam_size: 搜索宽度，越大越准但越慢，5 是推荐值
            beam_size=5,
            # vad_filter: 自动过滤掉没人说话的静音段
            vad_filter=True,
        )

        # "列表推导式"——Python 的一行循环写法
        # 等同于：
        #   results = []
        #   for seg in segments:
        #       results.append({...})
        results = [
            {
                "start": round(seg.start, 2),   # 开始时间（秒），保留2位小数
                "end": round(seg.end, 2),        # 结束时间（秒）
                "text": seg.text.strip(),         # 文本内容，.strip() 去首尾空格
            }
            for seg in segments
        ]

        print(f"[Whisper] 转录完成！共 {len(results)} 个片段")
        if results:
            print(f"[Whisper] 首句: [{results[0]['start']:.1f}s] {results[0]['text']}")
            print(f"[Whisper] 末句: [{results[-1]['start']:.1f}s] {results[-1]['text']}")

        return results

    def transcribe_with_speakers(self, video_path: str) -> list[dict]:
        """
        带说话人识别的转录（实验性功能）

        Faster-Whisper 原生不支持说话人分离，这里做简单的启发式判断：
        - 如果两段文字之间间隔超过 1 秒，可能是不同人说话
        - 标记为不同的 speaker_id

        注意：真正的说话人分离需要用 pyannote-audio 等专用模型，
        这里只是简单版本，用于给 Agent 提供更多上下文。
        """
        segments = self.transcribe(video_path)
        current_speaker = 0
        for i, seg in enumerate(segments):
            if i > 0:
                gap = seg["start"] - segments[i - 1]["end"]
                if gap > 1.0:  # 间隔超过1秒，可能是换人了
                    current_speaker += 1
            seg["speaker_id"] = current_speaker
        return segments


# ============================================================
# 如果你直接运行这个文件（python whisper.py），会执行下面的测试代码
# 这是 Python 的约定写法：if __name__ == "__main__" 判断
# "这个文件是被直接运行的，还是被 import 的？"
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("Whisper 工具测试")
    print("=" * 60)

    # 创建工具实例
    tool = WhisperTool(model_size="tiny")
    # 注意：这里用 "tiny" 模型，因为只是测试，不需要大模型
    # 正式使用时改成 "large-v3"

    # 如果有测试视频就转录
    test_video = "data/test.mp4"
    if Path(test_video).exists():
        results = tool.transcribe(test_video)
        for r in results[:5]:  # 只打印前5条
            print(f"  [{r['start']:6.1f}s - {r['end']:6.1f}s] {r['text']}")
    else:
        print(f"（没有找到测试视频 {test_video}，跳过转录测试）")
        print("请放一个视频到 data/ 目录下，然后运行这个文件来测试")
