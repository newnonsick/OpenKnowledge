

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

class FunctionDefinition(BaseModel):

    name: str
    description: str
    parameters: Dict[str, Any] = Field(default_factory=dict)

class ToolDefinition(BaseModel):

    type: Literal["function"] = "function"
    function: FunctionDefinition

class FunctionCall(BaseModel):

    name: str
    arguments: str = "{}"

class ToolCall(BaseModel):

    id: str
    type: Literal["function"] = "function"
    function: FunctionCall

    index: Optional[int] = None

class ToolResult(BaseModel):

    tool_call_id: str
    name: str
    content: str
    is_error: bool = False

KNOWLEDGE_SEARCH_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "knowledge_search",
        "description": "Searches the shared long-term knowledge base using hybrid retrieval. Use it to find previously stored information or context that may be relevant to the current task, and to discover related existing knowledge before creating, updating, or deleting memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Natural-language description or keywords representing the information to find."
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Optional workspace identifier to scope search. When provided, searches that workspace together with globally shared knowledge. When omitted, searches the active session workspace together with global knowledge."
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of search results to return (default: 5, min: 1, max: 20).",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of exact tag filters to restrict results. Use only when relevant tag values are known; avoid guessing tags for general discovery searches."
                }
            },
            "required": ["query"]
        }
    }
}

KNOWLEDGE_GET_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "knowledge_get",
        "description": "Retrieves the complete content and metadata of a specific knowledge item, including its latest or a requested revision. Use it when search results do not provide enough detail or when the full current item is needed before modification.",
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The unique identifier (UUID) of the knowledge item to retrieve."
                },
                "version": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional specific version number to retrieve. If omitted, returns the latest active revision."
                }
            },
            "required": ["item_id"]
        }
    }
}

KNOWLEDGE_SAVE_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "knowledge_save",
        "description": "Creates and persists a new structured long-term knowledge item. Use it for genuinely new information with future value when no existing knowledge item adequately represents it.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "description": "A concise, descriptive title for the knowledge item."
                },
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The complete markdown or plain-text body of the knowledge item."
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Owning workspace identifier for this item. Defaults to the active session workspace."
                },
                "is_global": {
                    "type": "boolean",
                    "description": "Whether this item is globally shared and accessible across all workspaces. The item remains associated with its owning workspace.",
                    "default": False
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of tags for categorization and filtered retrieval."
                }
            },
            "required": ["title", "content"]
        }
    }
}

KNOWLEDGE_UPDATE_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "knowledge_update",
        "description": "Creates a new immutable revision of an existing knowledge item using optimistic concurrency control. Use it to refine, extend, correct, reorganize, consolidate, or supersede existing memory while preserving continuity.",
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The unique identifier (UUID) of the knowledge item to update."
                },
                "expected_version": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "The version number of the knowledge item that this update is based on. Update is rejected if the stored version in DB differs, preventing accidental overwrite of newer changes."
                },
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The complete markdown or plain-text content for the new revision. This replaces the previous content entirely; provide the full desired item rather than only the changed or appended portion."
                },
                "title": {
                    "type": "string",
                    "description": "Optional replacement title. If omitted, the existing title is preserved."
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional replacement tag list. If omitted, existing tags are preserved. Provide an empty list to remove all tags."
                },
                "is_global": {
                    "type": "boolean",
                    "description": "Optional visibility update. If omitted, the existing visibility scope is preserved."
                },
                "change_summary": {
                    "type": "string",
                    "description": "Optional brief explanation of what was changed in this revision."
                }
            },
            "required": ["item_id", "expected_version", "content"]
        }
    }
}

KNOWLEDGE_DELETE_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "knowledge_delete",
        "description": "Soft-deletes a knowledge item while preserving revision history for audit purposes. Use it when the item should no longer remain active, such as when it is obsolete, redundant, misleading, superseded by another canonical item, or explicitly requested to be removed.",
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The unique identifier (UUID) of the knowledge item to delete."
                },
                "expected_version": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional version check for optimistic concurrency control during deletion. If provided, deletion fails if the stored version differs."
                }
            },
            "required": ["item_id"]
        }
    }
}

INTERNAL_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    KNOWLEDGE_SEARCH_SCHEMA,
    KNOWLEDGE_GET_SCHEMA,
    KNOWLEDGE_SAVE_SCHEMA,
    KNOWLEDGE_UPDATE_SCHEMA,
    KNOWLEDGE_DELETE_SCHEMA,
]

INTERNAL_TOOL_NAMES: frozenset[str] = frozenset({
    "knowledge_search",
    "knowledge_get",
    "knowledge_save",
    "knowledge_update",
    "knowledge_delete",
})

def is_internal_tool(name: str) -> bool:

    return name in INTERNAL_TOOL_NAMES

def get_internal_tool_definitions() -> List[ToolDefinition]:

    definitions: List[ToolDefinition] = []
    for schema in INTERNAL_TOOL_SCHEMAS:
        fn = schema["function"]
        definitions.append(
            ToolDefinition(
                type="function",
                function=FunctionDefinition(
                    name=fn["name"],
                    description=fn["description"],
                    parameters=fn["parameters"],
                )
            )
        )
    return definitions
