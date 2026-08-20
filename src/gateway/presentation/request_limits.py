from fastapi import Request

from src.gateway.presentation.errors import protocol_error_response


class RequestBodyLimitMiddleware:
    def __init__(self, app, *, max_body_bytes: int) -> None:
        if max_body_bytes <= 0:
            raise ValueError("Request body limit must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        declared = request.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                await self._reject(request, scope, receive, send)
                return
            if declared_size < 0 or declared_size > self.max_body_bytes:
                await self._reject(request, scope, receive, send)
                return
        messages = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received += len(message.get("body", b""))
            if received > self.max_body_bytes:
                await self._reject(request, scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        index = 0

        async def replay():
            nonlocal index
            if index >= len(messages):
                return {"type": "http.request", "body": b"", "more_body": False}
            message = messages[index]
            index += 1
            return message

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(request: Request, scope, receive, send) -> None:
        response = protocol_error_response(
            request,
            413,
            "invalid_request_error",
            "request_body_too_large",
            "The request body exceeds the configured limit.",
        )
        await response(scope, receive, send)
