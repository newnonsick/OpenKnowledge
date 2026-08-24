from __future__ import annotations

import json
import secrets

import httpx

from tests.e2e.live.conftest import BASE_URL, pytestmark  # noqa: F401

ANTHROPIC_HEADERS = {"anthropic-version": "2023-06-01"}


def _client(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {api_key}", **ANTHROPIC_HEADERS},
        timeout=180.0,
    )


def test_messages_basic_round_trip(api_key: str):
    with _client(api_key) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "default",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": "Reply with exactly: GATEWAY-PONG"}],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert isinstance(body["content"], list) and body["content"]
    text = "".join(
        block.get("text") or ""
        for block in body["content"]
        if block.get("type") == "text"
    )
    assert "GATEWAY-PONG" in text
    assert body["stop_reason"] in {"end_turn", "stop_sequence", None}
    assert body["usage"]["output_tokens"] > 0


def test_messages_thinking_block_passthrough(api_key: str):
    with _client(api_key) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "default",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": "What is 12 plus 13? Think briefly, then answer."}],
            },
        )
    assert response.status_code == 200, response.text
    blocks = response.json()["content"]
    types = [block.get("type") for block in blocks]
    assert "text" in types
    thinking_blocks = [block for block in blocks if block.get("type") == "thinking"]
    if thinking_blocks:
        assert any((block.get("thinking") or "").strip() for block in thinking_blocks)


def test_messages_streams_anthropic_sse_events(api_key: str):
    with _client(api_key) as client:
        with client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": "default",
                "max_tokens": 4000,
                "stream": True,
                "messages": [{"role": "user", "content": "Count from 1 to 5, digits separated by spaces."}],
            },
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = []
            data_payloads = []
            current = None
            for line in response.iter_lines():
                if line.startswith("event:"):
                    current = line[len("event:"):].strip()
                    events.append(current)
                elif line.startswith("data:") and current is not None:
                    raw = line[len("data:"):].strip()
                    try:
                        data_payloads.append((current, json.loads(raw)))
                    except json.JSONDecodeError:
                        pass

    assert events[0] == "message_start"
    assert "content_block_delta" in events
    assert events[-1] == "message_stop"

    message_start = next(data for name, data in data_payloads if name == "message_start")
    assert message_start["message"]["role"] == "assistant"

    text = "".join(
        data.get("delta", {}).get("text") or ""
        for name, data in data_payloads
        if name == "content_block_delta"
    )
    assert text.strip(), "stream must carry text deltas"

    message_delta = [data for name, data in data_payloads if name == "message_delta"]
    assert message_delta and message_delta[-1]["delta"]["stop_reason"] in {"end_turn", "stop_sequence", None}


def test_messages_external_tool_use_round_trip(api_key: str):
    tools = [
        {
            "name": "get_weather",
            "description": "Get current weather for a city",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]
    with _client(api_key) as client:
        first = client.post(
            "/v1/messages",
            json={
                "model": "default",
                "max_tokens": 8000,
                "messages": [
                    {"role": "user", "content": "What is the weather in Tokyo? Use the weather tool."}
                ],
                "tools": tools,
            },
        )
        assert first.status_code == 200, first.text
        body = first.json()
        tool_blocks = [block for block in body["content"] if block.get("type") == "tool_use"]
        assert tool_blocks, f"expected a tool_use block; got: {[b.get('type') for b in body['content']]}"
        tool_block = tool_blocks[0]
        assert tool_block["name"] == "get_weather"
        assert "tokyo" in json.dumps(tool_block["input"]).lower()

        assistant_message = {"role": "assistant", "content": body["content"]}
        user_tool_result = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_block["id"],
                    "content": "22C, sunny in Tokyo",
                }
            ],
        }
        second = client.post(
            "/v1/messages",
            json={
                "model": "default",
                "max_tokens": 4000,
                "messages": [
                    {"role": "user", "content": "What is the weather in Tokyo? Use the weather tool."},
                    assistant_message,
                    user_tool_result,
                ],
                "tools": tools,
            },
        )
    assert second.status_code == 200, second.text
    final_text = "".join(
        block.get("text") or ""
        for block in second.json()["content"]
        if block.get("type") == "text"
    )
    assert "22" in final_text, "final answer must incorporate the tool result"


def test_messages_internal_knowledge_interception(api_key: str, admin_client):
    marker = "AMK" + secrets.token_hex(5)
    created = admin_client.post(
        "/api/v1/knowledge",
        json_body={
            "space_id": "global",
            "title": f"Team lunch preference {marker}",
            "content": (
                f"For team events the group always picks codename BERRY-{marker} "
                f"as the official lunch theme name."
            ),
            "tags": [],
        },
        idempotency_key=f"e2e-kn-{secrets.token_hex(8)}",
    )
    assert created.status_code == 201, created.text
    item = created.json()

    try:
        with _client(api_key) as client:
            response = client.post(
                "/v1/messages",
                json={
                    "model": "default",
                    "max_tokens": 4000,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                f"What is the official team lunch theme codename recorded in knowledge "
                                f"for {marker}? Reply with the codename only."
                            ),
                        }
                    ],
                },
            )
        assert response.status_code == 200, response.text
        text = "".join(
            block.get("text") or ""
            for block in response.json()["content"]
            if block.get("type") == "text"
        )
        assert marker in text, f"knowledge interception failed; got: {text[:200]}"
        assert not any(block.get("type") == "tool_use" for block in response.json()["content"])
    finally:
        admin_client.delete(
            f"/api/v1/knowledge/{item['id']}",
            params={"expected_version": item["version"]},
            idempotency_key=f"e2e-kd-{secrets.token_hex(8)}",
        )


def test_messages_rejects_invalid_max_tokens(api_key: str):
    with _client(api_key) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "default",
                "max_tokens": 0,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code in (400, 422), response.text
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
