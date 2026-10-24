"""LLM 客户端层（L3a）。

★ 三条硬约束（设计方案 §3.4/§3.5，执行手册 §2.2）：
1. **off 档零请求**：`build_client` 在 `effective_mode()==off` 时返回 `NullLLM`。
2. **三级降级** `json_schema → json_object → plain`，plain 是万能兜底；
   降级状态按 **(model, format)** 记忆，不能用全局布尔（不同模型支持不同，全局会连坐）。
3. **httpx 直连**（不引入 openai SDK），走 `utils/http.py:HttpClient`（唯一网络出口），
   测试用 respx 拦截。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from ai_scholar.config import RunMode, Settings, load_settings
from ai_scholar.utils.errors import LLMError
from ai_scholar.utils.http import HttpClient, client_from_settings
from ai_scholar.utils.logging import get_logger

log = get_logger(__name__)

# ★ 视为「该 response_format 不被服务端支持」的状态码 → 触发降级。
#   429/5xx 是临时错误，由 HttpClient 重试；重试耗尽后不应降级（不是格式问题）。
_UNSUPPORTED_STATUS = frozenset({400, 404, 422})


@dataclass
class LLMResponse:
    """一次 LLM 调用的结果（记账所需的 token / cache 信息都在这里）。"""

    content: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hit: bool = False


class NullLLM:
    """off 档客户端：**永不发请求**，`complete` 直接返回空结果。

    ★ 用途：`effective_mode()==off` 时由 `build_client` 返回；即使调用方误调
    `complete` 也不会产生任何网络请求。
    """

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
        json_mode: bool = True,
    ) -> LLMResponse:
        return LLMResponse()

    async def aclose(self) -> None:
        return None


class LLMClient:
    """OpenAI 兼容 `/chat/completions` 的 httpx 直连客户端（非流式）。"""

    def __init__(
        self,
        *,
        http: HttpClient,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout_s: float,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        # ★ 降级状态按 (model, format) 记忆：某模型不支持某格式只记这一条，不连坐其它模型
        self._format_support: dict[tuple[str, str], bool] = {}

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
        json_mode: bool = True,
    ) -> LLMResponse:
        """发一次补全。三级降级：`json_schema → json_object → plain`。

        ★ `json_mode=False` 用于**文本/Markdown 输出**（定级/深度分析），
        直接走 plain，不强制 JSON（否则 `json_object` 会与文本输出冲突）。
        """
        attempts: list[tuple[str, dict[str, Any] | None]] = []
        if json_mode:
            if json_schema is not None:
                attempts.append(("json_schema", json_schema))
            attempts.append(("json_object", None))
        attempts.append(("plain", None))

        for fmt, schema in attempts:
            if fmt != "plain" and self._format_support.get((self.model, fmt)) is False:
                continue  # 已确认该模型不支持此格式，跳过
            response = await self._request(messages, fmt, schema)
            if response.status_code == 200:
                return self._parse(response.json())
            if response.status_code in _UNSUPPORTED_STATUS and fmt != "plain":
                self._format_support[(self.model, fmt)] = False
                log.warning(
                    "llm.format_unsupported",
                    model=self.model,
                    format=fmt,
                    status=response.status_code,
                )
                continue  # 降级到下一格式
            raise LLMError(f"LLM 调用失败：HTTP {response.status_code}（format={fmt}）")
        raise LLMError("LLM 调用失败：无可用输出格式")

    async def _request(
        self, messages: list[dict[str, str]], fmt: str, schema: dict[str, Any] | None
    ) -> httpx.Response:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if fmt == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            }
        elif fmt == "json_object":
            body["response_format"] = {"type": "json_object"}
        return await self.http.post(
            self.endpoint,
            source="llm",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json_body=body,
            timeout=httpx.Timeout(None, connect=self.http.connect_timeout_s, read=self.timeout_s),
        )

    @staticmethod
    def _parse(payload: dict[str, Any]) -> LLMResponse:
        choices = payload.get("choices") or []
        content = ""
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
        usage = payload.get("usage") or {}
        return LLMResponse(
            content=content,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            cache_hit=int(usage.get("prompt_cache_hit_tokens") or 0) > 0,
        )

    async def aclose(self) -> None:
        await self.http.aclose()


def build_client(settings: Settings | None = None, *, tier: str = "fast") -> LLMClient | NullLLM:
    """由 Settings 构造客户端。off 档 ⇒ `NullLLM`；否则按 tier 选模型/超时/max_tokens。"""
    settings = settings or load_settings()
    if settings.effective_mode() == RunMode.OFF:
        return NullLLM()
    if tier == "strong":
        model = settings.llm.strong_model
        max_tokens = settings.llm.max_tokens_strong
        timeout_s = settings.llm.timeout_strong_s
    else:
        model = settings.llm.fast_model
        max_tokens = settings.llm.max_tokens_fast
        timeout_s = settings.llm.timeout_s
    return LLMClient(
        http=client_from_settings(settings),
        base_url=settings.llm.base_url,
        api_key=settings.llm.api_key,
        model=model,
        temperature=settings.llm.temperature,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
    )


def parse_json_content(content: str) -> Any:
    """从 LLM 输出里解析出 JSON（plain 兜底：剥代码围栏 / 取首尾花括号）。

    ★ 解析失败抛 `LLMError`（调用方降级跳过，不中断流水线）。
    """
    text = (content or "").strip()
    if not text:
        raise LLMError("LLM 输出为空")
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError("LLM 输出无法解析为 JSON")
