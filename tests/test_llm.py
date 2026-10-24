"""L3a-1：LLM 客户端层测试。

★ 全程 respx mock，不发真实网络；off 档断言 0 请求。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ai_scholar.config import LLMSettings, RunMode, Settings
from ai_scholar.llm import (
    LLMClient,
    LLMResponse,
    NullLLM,
    build_client,
    parse_json_content,
)
from ai_scholar.utils.errors import LLMError
from ai_scholar.utils.http import HttpClient

ENDPOINT = "https://api.test/v1/chat/completions"


def chat_completion(content: str, *, hit: int = 0, tokens_in: int = 10, tokens_out: int = 5):
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "prompt_cache_hit_tokens": hit,
        },
    }


def make_client(http: HttpClient, *, model: str = "m") -> LLMClient:
    return LLMClient(
        http=http,
        base_url="https://api.test/v1",
        api_key="sk-1",
        model=model,
        temperature=0.3,
        max_tokens=100,
        timeout_s=30.0,
    )


def test_build_client_off_returns_null_llm() -> None:
    settings = Settings(llm=LLMSettings(api_key="", base_url=""), mode=RunMode.FAST)
    assert isinstance(build_client(settings), NullLLM)


def test_build_client_fast_returns_llm_client() -> None:
    settings = Settings(llm=LLMSettings(api_key="k", base_url="https://api.deepseek.com"))
    client = build_client(settings)
    assert isinstance(client, LLMClient)
    assert client.model == "deepseek-v4-flash"


@respx.mock
async def test_null_llm_makes_no_request() -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=chat_completion("{}")))
    result = await NullLLM().complete([{"role": "user", "content": "hi"}])
    assert result == LLMResponse()
    assert route.call_count == 0


@respx.mock
async def test_llm_client_sends_auth_and_json_schema_body() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=chat_completion('{"ok": true}'))
    )
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    async with HttpClient() as http:
        result = await make_client(http).complete(
            [{"role": "user", "content": "answer in JSON"}], json_schema=schema
        )
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer sk-1"
    body = json.loads(request.content)
    assert body["model"] == "m"
    assert body["messages"] == [{"role": "user", "content": "answer in JSON"}]
    assert body["max_tokens"] == 100
    assert body["response_format"]["type"] == "json_schema"
    assert result.content == '{"ok": true}'
    assert result.tokens_in == 10 and result.tokens_out == 5


@respx.mock
async def test_cache_hit_flag_parsed_from_usage() -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=chat_completion("{}", hit=7)))
    async with HttpClient() as http:
        result = await make_client(http).complete([{"role": "user", "content": "json"}])
    assert result.cache_hit is True


@respx.mock
async def test_downgrade_json_schema_to_json_object_and_remember() -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.Response(400, json={"error": "json_schema unsupported"}),
            httpx.Response(200, json=chat_completion('{"x": 1}')),
        ]
    )
    async with HttpClient() as http:
        client = make_client(http)
        result = await client.complete(
            [{"role": "user", "content": "json"}], json_schema={"type": "object"}
        )
    assert result.content == '{"x": 1}'
    assert route.call_count == 2
    # 降级状态按 (model, format) 记忆
    assert client._format_support[("m", "json_schema")] is False

    # 第二次调用跳过 json_schema，只发一次请求（json_object 直接 200）
    route.reset()
    route.mock(return_value=httpx.Response(200, json=chat_completion('{"x": 2}')))
    async with HttpClient() as http:
        client = make_client(http)
        # 预先注入记忆，验证跳过逻辑
        client._format_support[("m", "json_schema")] = False
        await client.complete([{"role": "user", "content": "json"}], json_schema={"type": "object"})
    assert route.call_count == 1


@respx.mock
async def test_downgrade_json_object_to_plain() -> None:
    """json_object 也不支持时降级到 plain（无 response_format），仍能返回内容。"""
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.Response(400, json={"error": "json_object unsupported"}),
            httpx.Response(200, json=chat_completion('```json\n{"x": 1}\n```')),
        ]
    )
    async with HttpClient() as http:
        result = await make_client(http).complete([{"role": "user", "content": "json"}])
    assert result.content == '```json\n{"x": 1}\n```'
    assert route.call_count == 2
    assert route.calls[1].request.content is not None


@respx.mock
async def test_complete_raises_when_all_formats_fail() -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    async with HttpClient() as http:
        with pytest.raises(LLMError):
            await make_client(http).complete([{"role": "user", "content": "json"}])


@respx.mock
async def test_json_mode_false_skips_response_format() -> None:
    """★ 文本/Markdown 输出（定级/深度分析）不强制 JSON：直接走 plain。"""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=chat_completion("plain text"))
    )
    async with HttpClient() as http:
        result = await make_client(http).complete(
            [{"role": "user", "content": "text"}], json_mode=False
        )
    body = json.loads(route.calls[0].request.content)
    assert "response_format" not in body
    assert route.call_count == 1
    assert result.content == "plain text"


def test_parse_json_content_variants() -> None:
    assert parse_json_content('{"a": 1}') == {"a": 1}
    assert parse_json_content('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_content('前缀 {"a": 1} 后缀') == {"a": 1}


def test_parse_json_content_rejects_garbage() -> None:
    with pytest.raises(LLMError):
        parse_json_content("")
    with pytest.raises(LLMError):
        parse_json_content("no json here")
