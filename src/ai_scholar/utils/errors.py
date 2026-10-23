"""统一异常层级。

约定：项目内所有自定义异常都继承 :class:`AIScholarError`，上层（CLI / pipeline）
只需捕获这一个基类即可兜底，同时又能针对子类给出不同的降级策略。

★ 单源异常不得打断整条流水线（继承 aipulse 的静默失败治理）：
调用方捕获后转为 warning，退出码不受影响。
"""

from __future__ import annotations


class AIScholarError(Exception):
    """项目所有异常的基类。"""


class ConfigError(AIScholarError):
    """配置缺失、格式错误或取值非法。"""


class SourceError(AIScholarError):
    """单个数据源采集失败。

    采集层必须捕获并转成该异常（携带 source），由编排层记录 warning 后继续跑其余源。
    """

    def __init__(self, source: str, message: str, *, cause: BaseException | None = None) -> None:
        self.source = source
        self.message = message
        self.cause = cause
        super().__init__(f"[{source}] {message}")


class UnsafeOutputPath(ConfigError):
    """导出目标路径危险（例如落在代码仓库内）。★ 拒绝写入，不做降级。"""


class StoreError(AIScholarError):
    """数据库读写失败。"""


class LLMError(AIScholarError):
    """LLM 调用失败或输出校验失败（调用方应降级跳过，而不是中断）。"""
