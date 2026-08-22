from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient


@pytest.mark.live_provider
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_PROVIDER_TESTS", "").lower() not in {"1", "true", "yes"},
    reason="Live provider tests require RUN_LIVE_PROVIDER_TESTS=true",
)
async def test_live_llm_provider_returns_a_canonical_completion() -> None:
    client = HttpLLMClient()
    try:
        response = await client.generate(
            [{"role": "user", "content": "Reply with the single word READY."}],
            temperature=0.0,
            max_tokens=128,
        )
    finally:
        await HttpLLMClient.close_shared_client()
    assert response.id
    assert response.model
    assert response.content.strip() == "READY"
    assert response.finish_reason in {"stop", "max_tokens"}
    evidence_path = os.getenv("LIVE_PROVIDER_EVIDENCE_FILE")
    if evidence_path:
        path = Path(evidence_path)
        evidence = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        evidence["llm_model_id"] = response.model
        evidence["llm_contract_passed"] = True
        path.write_text(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
