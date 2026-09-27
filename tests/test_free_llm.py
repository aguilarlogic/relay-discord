"""OpenAICompatLLM against a real local HTTP server standing in for Gemini/Groq."""

import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from relay.llm import LLMError, OpenAICompatLLM

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"], "additionalProperties": False}


@pytest.fixture
async def provider():
    """Yields (base_url, state). state['reply'] = (status, body) controls the
    next response; state['requests'] records what was sent."""
    state: dict = {"requests": [], "reply": (200, {})}

    async def chat(request: web.Request) -> web.Response:
        state["requests"].append({"json": await request.json(), "auth": request.headers.get("Authorization")})
        status, body = state["reply"]
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    server = TestServer(app)
    await server.start_server()
    yield str(server.make_url("/v1")), state
    await server.close()


def completion(content: str, finish_reason: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 80},
    }


async def test_success_request_shape_and_usage(provider):
    base_url, state = provider
    state["reply"] = (200, completion('{"a": 1}'))
    async with aiohttp.ClientSession() as session:
        llm = OpenAICompatLLM(session, base_url=base_url, api_key="k", model="gemini-flash")
        result = await llm.call(system="sys", user="hello", schema=SCHEMA)
    assert result.data == {"a": 1} and (result.input_tokens, result.output_tokens) == (900, 80)
    sent = state["requests"][0]
    assert sent["auth"] == "Bearer k"
    assert sent["json"]["model"] == "gemini-flash"
    assert sent["json"]["response_format"]["type"] == "json_schema"
    assert json.dumps(SCHEMA) in sent["json"]["messages"][0]["content"]


@pytest.mark.parametrize(("mode", "expected"), [("json_object", {"type": "json_object"}), ("none", None)])
async def test_json_modes(provider, mode, expected):
    base_url, state = provider
    state["reply"] = (200, completion('```json\n{"a": 2}\n```'))
    async with aiohttp.ClientSession() as session:
        llm = OpenAICompatLLM(session, base_url=base_url, api_key=None, model="m", json_mode=mode)
        assert (await llm.call(system="s", user="u", schema=SCHEMA)).data == {"a": 2}
    sent = state["requests"][0]
    assert sent["json"].get("response_format") == expected
    assert sent["auth"] is None


@pytest.mark.parametrize(
    "reply",
    [
        (429, {"error": "quota"}),
        (500, {"error": "boom"}),
        (200, {"unexpected": True}),
        (200, completion('{"a": 1', finish_reason="length")),
        (200, completion("not json")),
    ],
)
async def test_failures_raise_llm_error(provider, reply):
    base_url, state = provider
    state["reply"] = reply
    async with aiohttp.ClientSession() as session:
        llm = OpenAICompatLLM(session, base_url=base_url, api_key="k", model="m")
        with pytest.raises(LLMError):
            await llm.call(system="s", user="u", schema=SCHEMA)


async def test_non_json_body_raises_llm_error():
    async def chat(request):
        return web.Response(text="<html>gateway error</html>", status=200)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            llm = OpenAICompatLLM(session, base_url=str(server.make_url("/v1")), api_key="k", model="m")
            with pytest.raises(LLMError):
                await llm.call(system="s", user="u", schema=SCHEMA)
    finally:
        await server.close()


async def test_unreachable_provider_raises_llm_error():
    async with aiohttp.ClientSession() as session:
        llm = OpenAICompatLLM(session, base_url="http://127.0.0.1:9/v1", api_key="k", model="m", timeout_seconds=2)
        with pytest.raises(LLMError):
            await llm.call(system="s", user="u", schema=SCHEMA)
