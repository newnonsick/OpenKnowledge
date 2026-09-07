from src.gateway.config import RuntimeEnvironment, get_settings

CSRF_COOKIE_NAME = "openknowledge-csrf"

_PRODUCTION_ACCESS_COOKIE = "__Host-openknowledge-access"
_PRODUCTION_REFRESH_COOKIE = "__Secure-openknowledge-refresh"
_DEVELOPMENT_ACCESS_COOKIE = "openknowledge-access"
_DEVELOPMENT_REFRESH_COOKIE = "openknowledge-refresh"

CANONICAL_ACCESS_COOKIE = _PRODUCTION_ACCESS_COOKIE


def cookies_secure() -> bool:
    return get_settings().gateway.environment is not RuntimeEnvironment.DEVELOPMENT


def access_cookie_name() -> str:
    if cookies_secure():
        return _PRODUCTION_ACCESS_COOKIE
    return _DEVELOPMENT_ACCESS_COOKIE


def refresh_cookie_name() -> str:
    if cookies_secure():
        return _PRODUCTION_REFRESH_COOKIE
    return _DEVELOPMENT_REFRESH_COOKIE
