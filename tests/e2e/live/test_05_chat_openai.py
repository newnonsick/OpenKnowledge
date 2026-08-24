from __future__ import annotations

import json
import secrets

import httpx

from tests.e2e.live.conftest import BASE_URL, pytestmark  # noqa: F401


def _client(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=180.0,
    )


def test_chat_completion_basic_round_trip(api_key: str):
    with _client(api_key) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert "PONG" in (choice["message"]["content"] or "")
    assert choice["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] > 0
    assert body["model"]


def test_chat_completion_supports_system_and_multi_turn(api_key: str):
    with _client(api_key) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [
                    {"role": "system", "content": "You are a calculator. Answer with digits only."},
                    {"role": "user", "content": "What is 17 * 23?"},
                ],
                "temperature": 0.0,
            },
        )
    assert response.status_code == 200, response.text
    content = response.json()["choices"][0]["message"]["content"] or ""
    assert "391" in content


def test_chat_completion_streams_sse_events(api_key: str):
    with _client(api_key) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Count from 1 to 5, digits separated by spaces."}],
                "stream": True,
            },
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = []
            done_sentinel = False
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    done_sentinel = True
                    break
                events.append(json.loads(payload))
    assert done_sentinel, "SSE stream must end with [DONE]"
    assert events, "stream must carry chunk events"
    first = events[0]
    assert first["object"] == "chat.completion.chunk"
    assert first["choices"][0]["delta"].get("role") in {"assistant", None}
    joined = "".join(
        chunk["choices"][0]["delta"].get("content") or ""
        for chunk in events
        if chunk.get("choices")
    )
    assert joined.strip(), "stream must produce text content"
    final_reasons = [
        chunk["choices"][0].get("finish_reason")
        for chunk in events
        if chunk.get("choices") and chunk["choices"][0].get("finish_reason")
    ]
    assert final_reasons, "stream must include a finish_reason"


def test_internal_knowledge_tool_interception_is_transparent(api_key: str, admin_client):
    marker = "KNW" + secrets.token_hex(5)
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
                "/v1/chat/completions",
                json={
                    "model": "default",
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                f"What is the official team lunch theme codename recorded in knowledge "
                                f"for {marker}? Answer with the codename only."
                            ),
                        }
                    ],
                },
            )
        assert response.status_code == 200, response.text
        message = response.json()["choices"][0]["message"]
        content = message.get("content") or ""
        assert marker in content, f"gateway must resolve knowledge internally; got: {content[:200]}"
        assert not message.get("tool_calls"), "internal gateway tools must never leak to the client"
        assert response.json()["choices"][0]["finish_reason"] == "stop"
    finally:
        admin_client.delete(
            f"/api/v1/knowledge/{item['id']}",
            params={"expected_version": item["version"]},
            idempotency_key=f"e2e-kd-{secrets.token_hex(8)}",
        )


def test_external_tool_passthrough_round_trip(api_key: str):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]
    with _client(api_key) as client:
        first = client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [
                    {"role": "user", "content": "What is the weather in Paris right now? Use the weather tool."}
                ],
                "tools": tools,
            },
        )
        assert first.status_code == 200, first.text
        message = first.json()["choices"][0]["message"]
        assert message.get("tool_calls"), "model must request the external tool"
        call = message["tool_calls"][0]
        assert call["function"]["name"] == "get_weather"
        arguments = json.loads(call["function"]["arguments"])
        assert "paris" in arguments.get("city", "").lower()

        second = client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [
                    {"role": "user", "content": "What is the weather in Paris right now? Use the weather tool."},
                    message,
                    {"role": "tool", "tool_call_id": call["id"], "content": "18C, light rain in Paris"},
                ],
                "tools": tools,
            },
        )
    assert second.status_code == 200, second.text
    final_content = second.json()["choices"][0]["message"].get("content") or ""
    assert "18" in final_content, "final answer must incorporate the tool result"


def test_streaming_completion_with_external_tool_passthrough(api_key: str):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_ticket",
                "description": "Look up a support ticket status",
                "parameters": {
                    "type": "object",
                    "properties": {"ticket_id": {"type": "string"}},
                    "required": ["ticket_id"],
                },
            },
        }
    ]
    with _client(api_key) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [
                    {"role": "user", "content": "Check ticket status for ticket TCK-777 using the lookup tool."}
                ],
                "tools": tools,
                "stream": True,
            },
        ) as response:
            assert response.status_code == 200
            chunks = []
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                chunks.append(json.loads(payload))

    tool_call_fragments: dict[int, dict] = {}
    finish = None
    for chunk in chunks:
        if not chunk.get("choices"):
            continue
        delta = chunk["choices"][0].get("delta") or {}
        for call in delta.get("tool_calls") or []:
            index = call.get("index", 0)
            entry = tool_call_fragments.setdefault(index, {"id": "", "name": "", "arguments": ""})
            entry["id"] += call.get("id") or ""
            if call.get("function", {}).get("name"):
                entry["name"] += call["function"]["name"]
            entry["arguments"] += call.get("function", {}).get("arguments") or ""
        if chunk["choices"][0].get("finish_reason"):
            finish = chunk["choices"][0]["finish_reason"]

    assert tool_call_fragments, "streaming response must surface the external tool call"
    assembled = tool_call_fragments[0]
    assert assembled["name"] == "lookup_ticket"
    assert "TCK-777" in assembled["arguments"]
    assert finish == "tool_calls"


def test_chat_rejects_invalid_payload(api_key: str):
    with _client(api_key) as client:
        missing_messages = client.post(
            "/v1/chat/completions",
            json={"model": "default"},
        )
        empty_messages = client.post(
            "/v1/chat/completions",
            json={"model": "default", "messages": []},
        )
        unknown_model = client.post(
            "/v1/chat/completions",
            json={"model": "no-such-model-xyz", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert missing_messages.status_code == 400, missing_messages.text
    assert missing_messages.json()["error"]["type"] == "invalid_request_error"

    assert unknown_model.status_code == 404, unknown_model.text
    assert unknown_model.json()["error"]["code"] == "model_not_found"

    assert empty_messages.status_code == 400, empty_messages.text
    assert "messages" in empty_messages.json()["error"]["message"].lower()
