from datetime import date
import os

import pytest


LEGACY_CONTRACT_OWNER = "platform"
LEGACY_CONTRACT_EXPIRES = "2026-10-31"
LEGACY_CONTRACT_NODE_IDS = frozenset(
    {
        "tests/e2e/tier1_features/test_f03_models.py::test_f03_database_table_creation_and_insertion",
        "tests/e2e/tier1_features/test_f07_openai_api.py::test_f07_openai_chat_completions_non_streaming_json",
        "tests/e2e/tier1_features/test_f07_openai_api.py::test_f07_openai_chat_completions_streaming_sse",
        "tests/e2e/tier1_features/test_f07_openai_api.py::test_f07_openai_temperature_and_max_tokens",
        "tests/e2e/tier1_features/test_f07_openai_api.py::test_f07_openai_error_response_structure",
        "tests/e2e/tier1_features/test_f08_anthropic_api.py::test_f08_anthropic_messages_non_streaming_json",
        "tests/e2e/tier1_features/test_f08_anthropic_api.py::test_f08_anthropic_messages_tool_use_content_block",
        "tests/e2e/tier1_features/test_f08_anthropic_api.py::test_f08_anthropic_system_prompt_handling",
        "tests/e2e/tier1_features/test_f08_anthropic_api.py::test_f08_anthropic_token_usage_accounting",
        "tests/e2e/tier1_features/test_f08_anthropic_api.py::test_f08_anthropic_error_envelope_format",
        "tests/e2e/tier1_features/test_f13_occ.py::test_f13_occ_database_transactional_version_check",
        "tests/e2e/tier1_features/test_f14_soft_delete.py::test_f14_soft_delete_retains_revision_history",
        "tests/e2e/tier1_features/test_f15_tool_schemas.py::test_f15_knowledge_search_schema_parameters",
        "tests/e2e/tier1_features/test_f29_thinking_passthrough.py::test_f29_openai_nonstream_reasoning_content",
        "tests/e2e/tier1_features/test_f29_thinking_passthrough.py::test_f29_openai_stream_reasoning_deltas",
        "tests/e2e/tier1_features/test_f29_thinking_passthrough.py::test_f29_anthropic_nonstream_thinking_block",
        "tests/e2e/tier1_features/test_f29_thinking_passthrough.py::test_f29_anthropic_stream_thinking_delta_events",
        "tests/e2e/tier2_boundaries/test_f03_models_boundaries.py::test_f03_boundary_knowledge_revision_unique_version_constraint",
        "tests/e2e/tier2_boundaries/test_f07_openai_api_boundaries.py::test_f07_boundary_upstream_error_propagation",
        "tests/e2e/tier2_boundaries/test_f07_openai_api_boundaries.py::test_f07_boundary_sse_streaming_empty_and_rapid_chunks",
        "tests/e2e/tier2_boundaries/test_f08_anthropic_api_boundaries.py::test_f08_boundary_anthropic_tool_use_content_blocks",
        "tests/e2e/tier2_boundaries/test_f08_anthropic_api_boundaries.py::test_f08_boundary_anthropic_error_forwarding",
        "tests/e2e/tier2_boundaries/test_f19_embeddings_boundaries.py::test_f19_boundary_embedding_client_upstream_error",
        "tests/e2e/tier3_combinations/test_pairwise_auth_protocols.py::test_pairwise_f07_f10_auth_openai_bearer_token_json_and_stream",
        "tests/e2e/tier3_combinations/test_pairwise_auth_protocols.py::test_pairwise_f08_f10_auth_anthropic_x_api_key_json_and_stream",
        "tests/e2e/tier3_combinations/test_pairwise_auth_protocols.py::test_pairwise_f10_f07_f08_auth_dual_header_acceptance",
        "tests/e2e/tier3_combinations/test_pairwise_streaming_tools.py::test_pairwise_f07_f26_streaming_pure_text_sse_sequence",
        "tests/e2e/tier3_combinations/test_pairwise_streaming_tools.py::test_pairwise_f25_f26_streaming_external_tool_passthrough",
        "tests/e2e/tier3_combinations/test_pairwise_streaming_tools.py::test_pairwise_f24_f26_streaming_internal_tool_buffer_and_interception",
        "tests/e2e/tier4_scenarios/test_scenario_anthropic_claude_workflow.py::test_scenario_anthropic_external_tool_passthrough_loop",
        "tests/e2e/tier4_scenarios/test_scenario_anthropic_claude_workflow.py::test_scenario_anthropic_multi_tool_call_in_single_turn",
        "tests/e2e/tier4_scenarios/test_scenario_rag_knowledge_qa.py::test_scenario_f16_f20_f21_f22_f23_rag_knowledge_qa_complete_lifecycle",
        "tests/e2e/tier4_scenarios/test_scenario_rag_knowledge_qa.py::test_scenario_f23_rag_knowledge_qa_multi_document_blended_context",
        "tests/e2e/tier4_scenarios/test_scenario_streaming_sse_developer_ui.py::test_scenario_streaming_sse_ui_token_reassembly",
        "tests/e2e/tier4_scenarios/test_scenario_streaming_sse_developer_ui.py::test_scenario_streaming_sse_tool_call_delta_reassembly",
        "tests/e2e/tier5_adversarial/test_e2e_adversarial_matrix.py::TestAdversarialProtocolParity::test_openai_external_tool_passthrough_structure",
        "tests/e2e/tier5_adversarial/test_e2e_adversarial_matrix.py::TestAdversarialProtocolParity::test_anthropic_external_tool_passthrough_structure",
        "tests/e2e/tier5_adversarial/test_e2e_adversarial_matrix.py::TestAdversarialConcurrencyStress::test_20_concurrent_chat_completions_requests",
        "tests/e2e/tier5_adversarial/test_e2e_adversarial_matrix.py::TestAdversarialConcurrencyStress::test_20_concurrent_anthropic_messages_requests",
        "tests/e2e/tier5_adversarial/test_e2e_adversarial_matrix.py::TestAdversarialHTTPFileUpload::test_upload_corrupted_json_returns_422_validation_error",
        "tests/e2e/tier5_adversarial/test_e2e_adversarial_matrix.py::TestAdversarialHTTPFileUpload::test_upload_corrupted_pdf_returns_422_validation_error",
        "tests/e2e/tier5_adversarial/test_m2_streaming_and_failures.py::test_adv_anthropic_streaming_midstream_error_recovery",
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    expires = date.fromisoformat(LEGACY_CONTRACT_EXPIRES)
    if date.today() > expires:
        raise pytest.UsageError(
            f"legacy contract quarantine expired on {LEGACY_CONTRACT_EXPIRES}"
        )

    normalized = {item: item.nodeid.replace("\\", "/") for item in items}
    premarked = {
        node_id
        for item, node_id in normalized.items()
        if item.get_closest_marker("legacy_contract") is not None
    }
    unexpected = premarked - LEGACY_CONTRACT_NODE_IDS
    if unexpected:
        raise pytest.UsageError(
            "unauthorized legacy_contract markers: " + ", ".join(sorted(unexpected))
        )

    if os.getenv("LEGACY_CONTRACT_ENFORCE_ALLOWLIST") == "1":
        missing = LEGACY_CONTRACT_NODE_IDS - set(normalized.values())
        if missing:
            raise pytest.UsageError(
                "missing legacy contract nodes: " + ", ".join(sorted(missing))
            )
        os.environ["LEGACY_CONTRACT_ENFORCE_ALLOWLIST"] = "validated"

    for item in items:
        if normalized[item] in LEGACY_CONTRACT_NODE_IDS:
            item.add_marker(pytest.mark.legacy_contract)
