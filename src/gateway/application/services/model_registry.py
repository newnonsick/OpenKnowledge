

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.gateway.config import get_settings
from src.gateway.domain.exceptions import ModelNotFoundException

logger = logging.getLogger(__name__)

class ModelRegistryService:

    def __init__(
        self,
        default_model: Optional[str] = None,
        aliases: Optional[Dict[str, str]] = None,
        models_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        live = get_settings()
        self.default_model = (
            default_model
            if default_model is not None
            else live.llm.model_id
        )

        self.aliases: Dict[str, str] = {
            "default": self.default_model,
        }
        if aliases:
            self.aliases.update(aliases)

        self.models_metadata: Dict[str, Dict[str, Any]] = {
            self.default_model: {
                "context_window": live.llm.context_window,
                "owned_by": "gateway",
                "description": "Default gateway model configured via LLM_MODEL_ID",
            },
        }
        if models_metadata:
            self.models_metadata.update(models_metadata)

    def register_alias(self, alias: str, target_model: str) -> None:

        self.aliases[alias.strip()] = target_model.strip()

    def register_model(
        self,
        model_id: str,
        context_window: int = 8192,
        owned_by: str = "custom",
        description: str = "",
        **extra: Any,
    ) -> None:

        self.models_metadata[model_id] = {
            "context_window": context_window,
            "owned_by": owned_by,
            "description": description,
            **extra,
        }

    def resolve(self, requested_model: Optional[str] = None) -> str:

        if not requested_model or not str(requested_model).strip():
            return self.default_model

        cleaned = str(requested_model).strip()

        if cleaned in self.aliases:
            return self.aliases[cleaned]

        for alias, target in self.aliases.items():
            if alias.lower() == cleaned.lower():
                return target

        if cleaned in self.models_metadata:
            return cleaned

        if cleaned != self.default_model:
            logger.debug(
                "Model '%s' is not registered; falling back to default model '%s'.",
                cleaned,
                self.default_model,
            )
        return self.default_model

    def get_model_info(self, model_name: str) -> Dict[str, Any]:

        if not model_name or not str(model_name).strip():
            resolved = self.default_model
        else:
            resolved = self.resolve(model_name)

        if not resolved:
            raise ModelNotFoundException(f"The model '{model_name}' does not exist.")

        meta = self.models_metadata.get(resolved, {})
        owned_by = meta.get("owned_by", "gateway")

        return {
            "id": model_name,
            "object": "model",
            "created": 1700000000,
            "owned_by": owned_by,
            "context_window": meta.get("context_window", get_settings().llm.context_window),
            "root": resolved if resolved != model_name else None,
        }

    def list_models(self) -> List[Dict[str, Any]]:

        result: List[Dict[str, Any]] = []

        for model_id, meta in self.models_metadata.items():
            result.append({
                "id": model_id,
                "object": "model",
                "created": 1700000000,
                "owned_by": meta.get("owned_by", "gateway"),
                "context_window": meta.get("context_window", get_settings().llm.context_window),
            })

        for alias, target in self.aliases.items():

            if alias not in self.models_metadata:
                result.append({
                    "id": alias,
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": "gateway-alias",
                    "root": target,
                })

        return result

_model_registry_instance: Optional[ModelRegistryService] = None

def get_model_registry() -> ModelRegistryService:

    global _model_registry_instance
    current_default = get_settings().llm.model_id
    if (
        _model_registry_instance is None
        or _model_registry_instance.default_model != current_default
    ):
        _model_registry_instance = ModelRegistryService(default_model=current_default)
    return _model_registry_instance
