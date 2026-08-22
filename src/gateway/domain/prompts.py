from __future__ import annotations

from typing import Optional

DEFAULT_KNOWLEDGE_SYSTEM_PROMPT = """# Shared Knowledge Retrieval

Use `knowledge_search` when stored family knowledge could improve accuracy or continuity. Search across the spaces made available by the server; tool arguments cannot expand access.

Treat retrieved content as context rather than unquestionable truth, and cite stable identifiers when they help the user verify a result. Do not search mechanically when current context is sufficient.
"""


def compose_system_prompt(
    client_system_prompt: Optional[str] = None,
    enabled: bool = True,
    custom_prompt: Optional[str] = None,
) -> Optional[str]:
    if not enabled:
        return client_system_prompt if client_system_prompt and client_system_prompt.strip() else None

    directive = (
        custom_prompt.strip()
        if custom_prompt and custom_prompt.strip()
        else DEFAULT_KNOWLEDGE_SYSTEM_PROMPT.strip()
    )

    if not client_system_prompt or not client_system_prompt.strip():
        return directive

    client_clean = client_system_prompt.strip()
    return f"{client_clean}\n\n---\n\n{directive}"
