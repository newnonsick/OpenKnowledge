

from .chat_completions import router as chat_completions_router
from .files import router as files_router
from .health import router as health_router
from .messages import router as messages_router
from .management_auth import router as management_auth_router
from .management import router as management_router
from .models import router as models_router

__all__ = [
    "health_router",
    "models_router",
    "chat_completions_router",
    "messages_router",
    "management_auth_router",
    "management_router",
    "files_router",
]
