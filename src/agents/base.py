"""
===========================================================================
base.py — Agent 基类
===========================================================================
功能：所有 Agent 共享的基础功能（日志、计时、异常处理）

为什么要有基类？
  4 个 Agent 有很多相同的需求：都要记日志、都要计时、
  都要处理错误。如果每个 Agent 都写一遍这些代码，
  那就是"重复造轮子"。基类把这些公共功能抽出来，
  每个 Agent 直接继承就能用。

Python 知识点：
  1. 继承（inheritance）：
     class RequirementAgent(BaseAgent) → RequirementAgent "是一个" BaseAgent
     子类自动拥有父类的所有方法
  2. super().__init__()：调用父类的初始化方法
  3. ABC (Abstract Base Class)：抽象基类，不能直接实例化
     它的作用是定义"接口"，强制子类实现特定方法
===========================================================================
"""

import time
import logging
from abc import ABC, abstractmethod
from datetime import datetime

# 配置 logging：同时输出到控制台和文件
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)


class BaseAgent(ABC):
    """
    所有 Agent 的基类

    提供了：
      - self.log：统一的日志记录
      - self._start_timer / self._end_timer：计时功能
      - run() 抽象方法：每个 Agent 必须实现
    """

    def __init__(self, name: str):
        """
        初始化 Agent

        参数：
          name: Agent 的名字，如 "RequirementAgent"
        """
        self.name = name
        self.logger = logging.getLogger(name)
        self._start_time = None

    def log(self, message: str, level: str = "info"):
        """
        记录日志

        日志级别（严重程度从低到高）：
          DEBUG   — 调试信息，开发时用
          INFO    — 一般信息，正常流程
          WARNING — 警告，有问题但不影响运行
          ERROR   — 错误，某一步失败了
        """
        if level == "debug":
            self.logger.debug(message)
        elif level == "warning":
            self.logger.warning(message)
        elif level == "error":
            self.logger.error(message)
        else:
            self.logger.info(message)

    def _start_timer(self):
        """开始计时（内部方法，下划线开头表示"私有"）"""
        self._start_time = time.time()

    def _end_timer(self) -> float:
        """
        结束计时，返回耗时（秒）

        Python 知识点：
          time.time() 返回从 1970年1月1日 到现在的秒数（Unix 时间戳）
          结束时间 - 开始时间 = 耗时
        """
        elapsed = time.time() - self._start_time
        self.log(f"耗时: {elapsed:.1f} 秒")
        return elapsed

    def _timestamp(self) -> str:
        """获取当前时间的字符串表示"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @abstractmethod
    def run(self, *args, **kwargs):
        """
        执行 Agent 的主逻辑（抽象方法）

        @abstractmethod 的意思是：
          子类必须实现这个方法，否则会报错。
          这就像"合同"——你继承 BaseAgent，就必须提供 run() 的实现。

        每个 Agent 的 run() 输入输出不同：
          Agent 1: run(user_input: str) -> VideoRequirement
          Agent 2: run(video_path: str, requirement: VideoRequirement) -> ContentAnalysis
          Agent 3: run(analysis: ContentAnalysis, requirement: VideoRequirement) -> EditScript
          Agent 4: run(script: EditScript, video_path: str) -> ExecutionResult
        """
        ...
