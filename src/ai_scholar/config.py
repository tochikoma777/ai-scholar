"""配置系统：YAML 为主 + 环境变量插值 + pydantic 校验。

约定：
1. 阈值 / 限速 / 源配置一律走 `config/*.yaml`，代码里禁止硬编码常量。
2. ★ YAML 里的**环境开关必须写成 `${ENV}` 占位符**（api_key / base_url / 档位）。
   原因（aipulse 踩过的坑）：`Settings(**yaml)` 构造时 init 参数优先级高于环境变量，
   写成字面量会把环境变量**静默压掉**。算法阈值可以留字面量。
3. 缺 api_key ⇒ `effective_mode()` 自动返回 `off`，**不报错退出**（退出码 0）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_scholar.models import (
    Availability,
    TimeSemantics,
    WatchlistConfig,
    WatchlistGroup,
    WatchlistHit,
)
from ai_scholar.utils.errors import ConfigError

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
ENV_PREFIX = "AI_SCHOLAR_"
_WORD_BOUNDARY_TEMPLATE = r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])"


@lru_cache(maxsize=512)
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """word-boundary 匹配模板（编译结果缓存：论文量上千时逐条重编译很贵）。"""
    return re.compile(_WORD_BOUNDARY_TEMPLATE.format(re.escape(keyword.lower())))


def project_root() -> Path:
    """仓库根目录（可用 `AI_SCHOLAR_ROOT` 覆盖，便于测试指向临时目录）。"""
    override = os.getenv("AI_SCHOLAR_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[2]


def default_config_dir() -> Path:
    return project_root() / "config"


class RunMode(StrEnum):
    """三档运行模式：off = 零 API 花费；fast = 便宜档；strong = 推理档。"""

    OFF = "off"
    FAST = "fast"
    STRONG = "strong"


class AppSettings(BaseModel):
    name: str = "ai-scholar"
    timezone: str = "Asia/Shanghai"
    data_dir: str = "data"
    log_level: str = "INFO"


class HttpSettings(BaseModel):
    timeout_connect_s: float = 5.0
    timeout_read_s: float = 20.0
    max_concurrency: int = 6
    user_agent: str = "ai-scholar/0.1 (+https://github.com/tochikoma777/ai-scholar)"
    max_retries: int = 3


class LLMSettings(BaseModel):
    enabled: bool = True
    provider: str = "deepseek"
    base_url: str = ""
    api_key: str = ""
    fast_model: str = "deepseek-chat"
    strong_model: str = "deepseek-reasoner"
    temperature: float = 0.3
    timeout_s: int = 120
    # strong 档走思维链，比 fast 慢得多，分开设（否则读超时会打断）
    timeout_strong_s: int = 180
    max_tokens_fast: int = 4000
    max_tokens_strong: int = 8000
    max_retries: int = 3

    @field_validator("base_url", "api_key", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Any) -> Any:
        """YAML 里 `${VAR}` 未设置时会被解析成 None，这里统一收敛成空串。"""
        return "" if value is None else value

    def max_tokens_for(self, model_tier: str) -> int:
        return self.max_tokens_strong if model_tier == "strong" else self.max_tokens_fast


class BudgetSettings(BaseModel):
    daily_limit_cny: float = 2.0
    price_fast_cny_per_1m: float = 1.0
    price_strong_cny_per_1m: float = 4.0


class SourceSettings(BaseModel):
    """单个数据源的采集参数（L0-A：arXiv / HF Daily Papers；S2 登记但不启用）。"""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    availability: Availability = "always"
    availability_reason: str = ""
    rate_limit: str = "1/1s"
    # ★ 时间语义三态：arXiv / HF 索引滞后数天，必须用 discovery（first_seen）过滤
    time_semantics: TimeSemantics = "event"
    endpoint: str = ""
    # ★ HF 专用：实际请求端点（默认官方，可被镜像覆盖）。写成 ${ENV} 占位符。
    hf_base_url: str = ""

    def hf_url(self) -> str:
        """HF 实际请求端点：优先 `hf_base_url`，回落 `endpoint`（兼容旧配置）。"""
        return self.hf_base_url or self.endpoint

    def is_available(self, env: Mapping[str, str] | None = None) -> bool:
        """当前环境下该源是否应采集。"""
        if self.availability == "always":
            return True
        return is_actions_env(env)

    def source_extra(self, key: str, default: Any = None) -> Any:
        """读取各源特有配置项（extra=allow 存下来的字段）。

        ★ 必须走 `model_extra` 而不是 `getattr`：后者会先命中同名**方法**
        （例如 `categories()` 方法 vs `categories:` 配置键），拿到一个 method 对象。
        """
        value = (self.model_extra or {}).get(key, None)
        return default if value is None else value

    def categories(self) -> list[str]:
        return list(self.source_extra("categories", []) or [])


class ExportSettings(BaseModel):
    """导出参数（★ 默认**不是**全量：§5.6 明确否掉，年增 18.8 万篇会撑爆 Obsidian）。"""

    default_limit: int = 20
    filename_max_len: int = 80
    # ★ vault 是独立 private 仓库，默认落在代码仓库之外。
    #   只准 `~/` 开头：写死家目录绝对路径会① 泄露用户名 ② 别人 clone 后路径不存在。
    out_dir: str = "~/projects/ai-scholar-vault"


class CollectSettings(BaseModel):
    """采集层的规模参数（★ 改这些值必须回头回归依赖上下文长度的环节）。"""

    # 每日两源各取多少条原始数据
    arxiv_max_results: int = 200
    hf_days: int = 1
    # ★ 默认回看天数的**兜底值**；实际优先取 `sources.arxiv.lookback_days`
    default_days: int = 7
    # 深度分析通道 A 的当日 Top N（★ 立刻执行，不看 Tier）
    deep_dive_top_n: int = 5


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mode: RunMode = RunMode.FAST
    app: AppSettings = Field(default_factory=AppSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    export: ExportSettings = Field(default_factory=ExportSettings)
    collect: CollectSettings = Field(default_factory=CollectSettings)
    sources: dict[str, SourceSettings] = Field(default_factory=dict)

    def effective_mode(self) -> RunMode:
        """实际运行档位：关闭 llm / 缺 api_key / 缺 base_url 时自动降级 off（不报错）。

        ★ base_url 必须和 api_key 一起参与判断（aipulse Actions 首跑炸出来的坑）：
        "配了 key 却漏配 base_url"的环境（典型就是 CI 只注入了 key 的 secret）
        会一路走到客户端构造处撞 ConfigError，整条流水线崩溃——违反"缺配置应降级"。
        """
        if not self.llm.enabled or not self.llm.api_key.strip() or not self.llm.base_url.strip():
            return RunMode.OFF
        return self.mode

    def data_dir(self) -> Path:
        path = Path(self.app.data_dir)
        return path if path.is_absolute() else project_root() / path

    def db_path(self) -> Path:
        return self.data_dir() / "ai_scholar.db"

    def source(self, name: str) -> SourceSettings | None:
        return self.sources.get(name)

    def lookback_days(self) -> int:
        """默认回看窗口（天）：优先 `sources.arxiv.lookback_days`，回落 `collect.default_days`。

        ★ 不硬编码：设计方案 §3.3 载明 arXiv 索引滞后 3–7 天，窗口要能按环境调。
        """
        arxiv = self.source("arxiv")
        if arxiv is not None:
            value = arxiv.source_extra("lookback_days")
            if value is not None:
                return int(value)
        return self.collect.default_days


def is_actions_env(env: Mapping[str, str] | None = None) -> bool:
    """是否运行在 GitHub Actions（`AI_SCHOLAR_ENV=actions`）。

    本地 WSL 与 Actions 的网络可达性不同：本地不可达的源标 `actions_only`，
    在本地跳过且不计失败，Actions 环境全开。
    """
    source = os.environ if env is None else env
    return str(source.get(f"{ENV_PREFIX}ENV", "")).strip().lower() == "actions"


def interpolate_env(text: str) -> str:
    """把 `${VAR}` / `${VAR:-default}` 替换成环境变量值（未设置时用默认值或空串）。"""
    return ENV_PATTERN.sub(
        lambda m: os.getenv(m.group(1), m.group(2) if m.group(2) is not None else ""),
        text,
    )


def load_yaml(path: Path) -> dict[str, Any]:
    """读 YAML 并做环境变量插值。文件不存在时返回空 dict（由调用方决定默认值）。"""
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(interpolate_env(raw)) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是 mapping：{path}")
    return data


@lru_cache(maxsize=8)
def load_settings(path: str | Path | None = None) -> Settings:
    """加载主配置。默认读 `config/default.yaml`。

    ★ 先 `load_dotenv()`：YAML 里的 `${VAR}` 是在 `load_yaml()` 里用 `os.environ`
    插值的，而 `Settings(**yaml)` 的 init 参数优先级高于 pydantic-settings 的
    `env_file`。不先把 .env 灌进环境，按 README 写进 .env 的 key 永远读不到。
    """
    load_dotenv(override=False)
    config_path = Path(path) if path else default_config_dir() / "default.yaml"
    return Settings(**load_yaml(config_path))


# ── watchlist 双文件（★ 本项目特有）────────────────────────────────────────


def watchlist_paths(config_dir: Path | None = None) -> tuple[Path, Path]:
    """返回 `(default_path, local_path)`。"""
    base = config_dir if config_dir is not None else default_config_dir()
    return base / "watchlist.yaml", base / "watchlist.local.yaml"


def _merge_groups(
    base: list[WatchlistGroup], override: list[WatchlistGroup]
) -> list[WatchlistGroup]:
    """同名组整体覆盖（weight + keywords 一起换），新组追加在后面。

    ★ 整组覆盖而非逐字段合并：半合并出来的"权重来自 A、关键词来自 B"
    会让命中分数无法解释。
    """
    merged: dict[str, WatchlistGroup] = {g.name: g for g in base}
    for g in override:
        merged[g.name] = g
    # 保持 default 的组顺序在前，local 新增组在后
    base_names = {g.name for g in base}
    order = [g.name for g in base] + [g.name for g in override if g.name not in base_names]
    return [merged[name] for name in dict.fromkeys(order)]


def load_watchlist(config_dir: Path | None = None) -> WatchlistConfig:
    """加载 watchlist：`watchlist.local.yaml` 深合并覆盖 `watchlist.yaml`。

    local 缺失项回落 default（整组覆盖，见 :func:`_merge_groups`）。
    两个文件都不存在时返回空配置（doctor 会给 warning，不是 error）。
    """
    default_path, local_path = watchlist_paths(config_dir)
    default_data = load_yaml(default_path)
    local_data = load_yaml(local_path)
    if not default_data and not local_data:
        return WatchlistConfig()
    base = WatchlistConfig(**default_data)
    if not local_data:
        return base
    override = WatchlistConfig(**local_data)
    return WatchlistConfig(
        default_threshold=override.default_threshold
        if "default_threshold" in local_data
        else base.default_threshold,
        groups=_merge_groups(base.groups, override.groups),
    )


def match_watchlist(
    watchlist: WatchlistConfig,
    title: str,
    abstract: str = "",
    *,
    threshold: float | None = None,
) -> WatchlistHit:
    """匹配一条论文。

    - 命中【布尔】：score ≥ threshold ⇒ 触发（决定要不要它）；
    - 命中【分】：Σ(命中组权重)，同组多次命中只计一次 ⇒ 仅用于排序。
    """
    limit = watchlist.default_threshold if threshold is None else threshold
    text = f"{title}\n{abstract}".lower()
    score = 0.0
    groups: list[str] = []
    for group in watchlist.groups:
        for keyword in group.keywords:
            if _keyword_pattern(keyword).search(text):
                score += group.weight
                groups.append(group.name)
                break  # 同组多次命中只计一次
    return WatchlistHit(matched=score >= limit, score=round(score, 4), groups=groups)
