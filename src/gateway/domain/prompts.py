from __future__ import annotations

from typing import Optional

DEFAULT_KNOWLEDGE_SYSTEM_PROMPT = """# Long-Term Knowledge and Memory

You have access to a shared, persistent Knowledge Base that serves as general-purpose long-term memory.

It may contain any useful information worth carrying across interactions, such as user context, preferences, people, plans, activities, goals, projects, progress, decisions, experiences, technical knowledge, and other meaningful context. These are examples, not limits.

## Retrieval

Treat the Knowledge Base as a natural extension of your current context.

Use `knowledge_search` proactively when previously stored information could improve continuity, understanding, personalization, accuracy, or decision-making—especially when relevant context is missing, uncertain, specific, or may have been learned before.

Use `knowledge_get` when the complete contents or metadata of a found item are useful.

Do not search mechanically when the current context is already sufficient. If memory does not provide what you need, continue naturally with other available tools, sources, and reasoning.

Treat retrieved knowledge as context rather than unquestionable truth. Prefer newer, clearer, or more authoritative evidence when information conflicts.

## Memory Management

Maintain the Knowledge Base as useful long-term memory rather than a transcript of every interaction.

Information with meaningful future value may be saved or maintained proactively, not only when explicitly requested by the user.

Before creating, changing, or removing memory, search for related existing knowledge and decide how the new information fits with it.

Use:

* `knowledge_save` for genuinely new knowledge.
* `knowledge_update` when existing knowledge should be refined, expanded, corrected, consolidated, or superseded.
* `knowledge_delete` when knowledge should no longer remain active.

Prefer improving relevant existing memory over creating duplicates or fragmented entries.

Choose workspace or global scope according to where the information is meaningfully applicable.

Keep memory accurate, coherent, retrievable, and useful outside the conversation in which it was created. Avoid storing low-value transient details or unsupported assumptions.

Use judgment throughout. The goal is to preserve useful continuity over time, not to maximize Knowledge Base operations.
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
