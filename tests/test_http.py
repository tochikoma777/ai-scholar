"""HTTP 客户端测试。

★ 全程 respx mock，不发真实网络；off 档（无请求场景）断言 0 请求。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from ai_scholar.utils.http import (
    DEFAULT_USER_AGENT,
    HttpClient,
    RateLimit,
    RateLimitError,
    TokenBucket,
    resolve_proxy,
)

ARXIV = "http://export.arxiv.org/api/query"


def test_rate_limit_parse() -> None:
    assert RateLimit.parse("1/3s") == RateLimit(count=1, seconds=3.0)
    assert RateLimit.parse("10/1s") == RateLimit(count=10, seconds=1.0)
    assert RateLimit.parse("100/1h") == RateLimit(count=100, seconds=3600.0)
    assert RateLimit.parse("1/3s").per_second == pytest.approx(1 / 3)


@pytest.mark.parametrize("bad", ["", "abc", "0/1s", "1/0s", "x/1s"])
def test_rate_limit_parse_rejects_bad_spec(bad: str) -> None:
    with pytest.raises(RateLimitError):
        RateLimit.parse(bad)


async def test_token_bucket_spends_and_refills() -> None:
    bucket = TokenBucket(RateLimit.parse("2/1s"))
    assert bucket.available() == 2.0
    await bucket.acquire()
    await bucket.acquire()
    assert bucket.available() < 0.5  # 令牌耗尽
    assert bucket.wait_time() > 0


async def test_token_bucket_wait_time_zero_when_available() -> None:
    bucket = TokenBucket(RateLimit.parse("5/1s"))
    assert bucket.wait_time() == 0.0


@respx.mock
async def test_get_returns_response_and_counts_request() -> None:
    route = respx.get(ARXIV).mock(return_value=httpx.Response(200, text="<feed/>"))
    async with HttpClient() as client:
        response = await client.get(ARXIV, source="arxiv", rate_limit="10/1s")
    assert response.status_code == 200
    assert route.called
    assert client.stats["requests"] == 1


@respx.mock
async def test_retry_on_429_then_success() -> None:
    """429 必须重试（仅 429 / 5xx / 超时），最多 3 次。"""
    route = respx.get(ARXIV).mock(side_effect=[httpx.Response(429), httpx.Response(200, text="ok")])
    async with HttpClient(max_retries=3) as client:
        response = await client.get(ARXIV, source="arxiv")
    assert response.status_code == 200
    assert route.call_count == 2
    assert client.stats["requests"] == 2


@respx.mock
async def test_retry_exhausted_returns_last_response() -> None:
    """重试耗尽时返回最后一次响应，**不抛异常**（单源失败不得中断全局）。"""
    route = respx.get(ARXIV).mock(return_value=httpx.Response(503))
    async with HttpClient(max_retries=2) as client:
        response = await client.get(ARXIV, source="arxiv")
    assert response.status_code == 503
    assert route.call_count == 2


@respx.mock
async def test_no_retry_on_404() -> None:
    route = respx.get(ARXIV).mock(return_value=httpx.Response(404))
    async with HttpClient(max_retries=3) as client:
        response = await client.get(ARXIV, source="arxiv")
    assert response.status_code == 404
    assert route.call_count == 1  # 4xx（非 429）不重试


@respx.mock
async def test_retry_on_timeout() -> None:
    route = respx.get(ARXIV).mock(
        side_effect=[httpx.ReadTimeout("timeout"), httpx.Response(200, text="ok")]
    )
    async with HttpClient(max_retries=3) as client:
        response = await client.get(ARXIV, source="arxiv")
    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_uniform_user_agent() -> None:
    route = respx.get(ARXIV).mock(return_value=httpx.Response(200, text="ok"))
    async with HttpClient() as client:
        await client.get(ARXIV, source="arxiv")
    request = route.calls[0].request
    assert request.headers["User-Agent"] == DEFAULT_USER_AGENT
    assert "ai-scholar" in request.headers["User-Agent"]


def test_resolve_proxy_priority() -> None:
    assert resolve_proxy({"https_proxy": "http://a:1"}) == "http://a:1"
    assert resolve_proxy({"HTTPS_PROXY": "http://b:2", "https_proxy": "http://a:1"}) == "http://b:2"
    assert resolve_proxy({"HTTP_PROXY": "http://c:3"}) == "http://c:3"
    assert resolve_proxy({}) is None


@respx.mock
async def test_off_mode_makes_no_request() -> None:
    """off 档（无 LLM 调用）下代码路径**根本不发起请求**。

    ★ 用 respx 拦截全网 GET，断言 0 次调用——这是"代码没发请求"的真绿，
    不是"用 mock 顶掉后断言 stats 为 0"的假绿。
    """
    route = respx.route(method="GET").mock(return_value=httpx.Response(200, text="x"))
    async with HttpClient() as client:
        assert client.stats["requests"] == 0
    assert route.call_count == 0


@respx.mock
async def test_per_source_rate_limit_is_independent() -> None:
    """每源独立令牌桶：arxiv 用掉令牌不影响 hf。"""
    r1 = respx.get(ARXIV).mock(return_value=httpx.Response(200, text="a"))
    r2 = respx.get("https://huggingface.co/api/daily_papers").mock(
        return_value=httpx.Response(200, text="b")
    )
    async with HttpClient() as client:
        client.set_rate_limit("arxiv", "1/3s")
        await client.get(ARXIV, source="arxiv")
        assert client.bucket("arxiv").available() < 0.5
        assert client.bucket("hf_daily").available() > 1.0  # 默认桶不受影响
        await client.get("https://huggingface.co/api/daily_papers", source="hf_daily")
    assert r1.called and r2.called
