"""统一 HTTP 客户端。

所有网络请求**必须**经过 :meth:`HttpClient.request`，这样：
1. 限速 / 重试 / UA / 代理只在一处实现，行为一致；
2. 测试里用 `respx` 拦截即可，无需打真实网络（CI 无密钥全绿的前提）。

特性：
- 每源独立令牌桶限速（`rate_limit` 形如 `1/3s`、`10/1s`）——arXiv 要求约 1 req / 3s
- 连接 / 读取超时可配；tenacity 重试：**仅 429 / 5xx / 超时**，最多 3 次，指数退避 + 抖动
- 统一 User-Agent 与 HTTP(S)_PROXY 支持

★ 本项目不引入 `openai` SDK，LLM 调用也走这里的 httpx 直连（自带 httpx 2.x 冲突
且导致 respx 无法 mock）。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ai_scholar.utils.logging import get_logger

log = get_logger(__name__)

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRY_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.DecodingError,
)

_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
DEFAULT_RATE_LIMIT = "10/1s"
DEFAULT_USER_AGENT = "ai-scholar/0.1 (+https://github.com/tochikoma777/ai-scholar)"


class RateLimitError(ValueError):
    """rate_limit 字符串无法解析。"""


@dataclass(frozen=True)
class RateLimit:
    """限速规格：`count/period`，如 `1/3s`、`10/1s`。"""

    count: int
    seconds: float

    @classmethod
    def parse(cls, spec: str) -> RateLimit:
        text = (spec or "").strip().lower()
        if "/" not in text:
            raise RateLimitError(f"限速格式应为 'N/period'，如 1/3s、10/1s，收到：{spec!r}")
        left, right = text.split("/", 1)
        try:
            count = int(left.strip())
        except ValueError as exc:
            raise RateLimitError(f"限速条数必须为整数：{spec!r}") from exc
        period = right.strip()
        if period and period[-1] in _DURATION_UNITS:
            number = period[:-1] or "1"
            seconds = float(number) * _DURATION_UNITS[period[-1]]
        else:
            try:
                seconds = float(period)
            except ValueError as exc:
                raise RateLimitError(f"限速周期无法解析：{spec!r}") from exc
        if count <= 0 or seconds <= 0:
            raise RateLimitError(f"限速参数必须为正：{spec!r}")
        return cls(count=count, seconds=seconds)

    @property
    def per_second(self) -> float:
        return self.count / self.seconds


class TokenBucket:
    """令牌桶：容量 = count，补充速率 = count/period。

    用令牌桶而非固定 sleep，是因为"攒了一段时间的配额"可以被突发请求一次性用掉，
    同时长期平均速率不会超限。
    """

    def __init__(self, limit: RateLimit, clock: Any = time.monotonic) -> None:
        self.limit = limit
        self.capacity = float(limit.count)
        self._tokens = float(limit.count)
        self._clock = clock
        self._updated = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.limit.per_second)
            self._updated = now

    def available(self) -> float:
        self._refill()
        return self._tokens

    def wait_time(self, tokens: float = 1.0) -> float:
        """取到 `tokens` 个令牌还需等待的秒数（0 表示立即可用）。"""
        self._refill()
        if self._tokens >= tokens:
            return 0.0
        return (tokens - self._tokens) / self.limit.per_second

    async def acquire(self, tokens: float = 1.0) -> None:
        """异步等待直到拿到令牌。"""
        while True:
            wait = self.wait_time(tokens)
            if wait <= 0:
                self._tokens -= tokens
                return
            await asyncio.sleep(min(wait, self.limit.seconds))


def resolve_proxy(env: Mapping[str, str] | None = None) -> str | None:
    """按优先级解析代理：HTTPS_PROXY > https_proxy > HTTP_PROXY > http_proxy。"""
    source = os.environ if env is None else env
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = source.get(key)
        if value:
            return value
    return None


def _is_retryable(response: httpx.Response) -> bool:
    return response.status_code in RETRY_STATUS


class _RetryableStatus(Exception):
    """可重试的 HTTP 状态码（429 / 5xx），用于把状态码接入 tenacity 的重试判定。"""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"HTTP {response.status_code}")


class HttpClient:
    """httpx.AsyncClient 的统一封装。所有采集请求都必须走 :meth:`request`。"""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        connect_timeout_s: float = 5.0,
        read_timeout_s: float = 20.0,
        max_retries: int = 3,
        max_concurrency: int = 6,
        proxy: str | None = None,
        default_headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.connect_timeout_s = connect_timeout_s
        self.read_timeout_s = read_timeout_s
        self.max_retries = max_retries
        self.max_concurrency = max_concurrency
        self.proxy = proxy if proxy is not None else resolve_proxy()
        self.stats: dict[str, int] = {"requests": 0, "retries": 0}

        headers = {"User-Agent": user_agent, "Accept": "*/*"}
        if default_headers:
            headers.update(dict(default_headers))

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(None, connect=connect_timeout_s, read=read_timeout_s),
                follow_redirects=True,
                proxy=self.proxy,
            )
            self._owns_client = True

        self._buckets: dict[str, TokenBucket] = {}
        self._rate_limits: dict[str, RateLimit] = {}

    # ── 限速 ────────────────────────────────────────────────────────────
    def set_rate_limit(self, source: str, spec: str) -> None:
        """为某个源设置限速，如 `set_rate_limit("arxiv", "1/3s")`。"""
        self._rate_limits[source] = RateLimit.parse(spec)
        self._buckets.pop(source, None)

    def bucket(self, source: str, spec: str | None = None) -> TokenBucket:
        """取（或按需创建）某源的令牌桶；未配置限速时用宽松默认值。"""
        if spec is not None:
            limit = RateLimit.parse(spec)
            if self._rate_limits.get(source) != limit:
                self._rate_limits[source] = limit
                self._buckets.pop(source, None)
        if source not in self._buckets:
            self._buckets[source] = TokenBucket(
                self._rate_limits.get(source, RateLimit.parse(DEFAULT_RATE_LIMIT))
            )
        return self._buckets[source]

    # ── 请求 ────────────────────────────────────────────────────────────
    async def request(
        self,
        method: str,
        url: str,
        *,
        source: str = "default",
        params: Mapping[str, str | int | float | bool | None] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        content: bytes | str | None = None,
        rate_limit: str | None = None,
        timeout: httpx.Timeout | None = None,
        max_retries: int | None = None,
    ) -> httpx.Response:
        """发一个请求：先取令牌，再走重试。**只重试 429 / 5xx / 超时**。"""
        await self.bucket(source, rate_limit).acquire()

        attempts = max(1, self.max_retries if max_retries is None else max_retries)
        request_timeout = timeout or httpx.Timeout(
            None, connect=self.connect_timeout_s, read=self.read_timeout_s
        )

        async def send_once() -> httpx.Response:
            self.stats["requests"] += 1
            response = await self._client.request(
                method,
                url,
                params=params,
                headers=dict(headers) if headers else None,
                json=json_body,
                content=content,
                timeout=request_timeout,
            )
            log.debug(
                "http.response",
                url=url,
                source=source,
                status=response.status_code,
                bytes=len(response.content),
            )
            if _is_retryable(response):
                raise _RetryableStatus(response)
            return response

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(attempts),
                wait=wait_exponential_jitter(initial=0.5, max=8.0),
                retry=retry_if_exception_type((_RetryableStatus, *RETRY_EXCEPTIONS)),
                reraise=True,
            ):
                with attempt:
                    return await send_once()
        except _RetryableStatus as exc:
            # 重试耗尽：返回最后一次响应，是否算失败由调用方决定（单源失败不得中断全局）
            log.warning(
                "http.retry_exhausted", url=url, source=source, status=exc.response.status_code
            )
            return exc.response
        raise AssertionError("unreachable")  # pragma: no cover

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def fetch_text(self, url: str, **kwargs: Any) -> str:
        response = await self.request("GET", url, **kwargs)
        return response.text

    async def fetch_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("GET", url, **kwargs)
        return response.json()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


def client_from_settings(settings: Any) -> HttpClient:
    """由 Settings 构造客户端（超时 / UA / 并发 / 代理全部来自配置，不硬编码）。"""
    return HttpClient(
        user_agent=settings.http.user_agent,
        connect_timeout_s=settings.http.timeout_connect_s,
        read_timeout_s=settings.http.timeout_read_s,
        max_retries=settings.http.max_retries,
        max_concurrency=settings.http.max_concurrency,
    )
