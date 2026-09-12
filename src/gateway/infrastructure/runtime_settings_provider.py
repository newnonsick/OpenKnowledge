from src.gateway.application.services.runtime_settings_service import EffectiveRuntimePolicy, RetrievalRuntimeSettings, RuntimeSettingsService
from src.gateway.domain.identity import Principal
from src.gateway.infrastructure.database import get_session_factory, principal_session


async def load_active_retrieval_settings(principal: Principal) -> RetrievalRuntimeSettings:
    async with principal_session(get_session_factory(), principal) as session:
        active = await RuntimeSettingsService(session).active()
        return active.values.retrieval


async def load_active_runtime_policy(principal: Principal) -> EffectiveRuntimePolicy:
    try:
        factory = get_session_factory()
    except RuntimeError:
        return EffectiveRuntimePolicy.default()
    try:
        async with principal_session(factory, principal) as session:
            active = await RuntimeSettingsService(session).active()
            return EffectiveRuntimePolicy.from_revision(active)
    except Exception:
        return EffectiveRuntimePolicy.default()
