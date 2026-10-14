"""Small ASGI request guards used before FastAPI parses multipart bodies."""

from __future__ import annotations

import json
import os
from typing import Awaitable, Callable


class RequestBodyTooLarge(Exception):
    """Raised by the guarded ASGI receive channel after the byte limit is crossed."""


def _configured_limit(env_name: str, default: int) -> int:
    try:
        value = int(os.getenv(env_name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class RequestBodyLimitMiddleware:
    """Enforce a route's raw body limit, including chunked multipart uploads.

    Checking ``Content-Length`` inside a FastAPI endpoint is too late: Starlette has
    already parsed and spooled the entire multipart body before the endpoint runs.
    This middleware wraps the ASGI receive channel, so oversized input is rejected
    while it is still being streamed from the server.
    """

    def __init__(
        self,
        app,
        *,
        path: str,
        env_name: str,
        default_limit: int,
    ) -> None:
        self.app = app
        self.path = path
        self.env_name = env_name
        self.default_limit = default_limit

    @staticmethod
    async def _send_json(send: Callable[..., Awaitable[None]], status: int, detail: str) -> None:
        body = json.dumps(
            {"detail": detail},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("path") != self.path:
            await self.app(scope, receive, send)
            return

        limit = _configured_limit(self.env_name, self.default_limit)
        raw_lengths = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if raw_lengths:
            try:
                lengths = {int(value.decode("ascii")) for value in raw_lengths}
            except (UnicodeDecodeError, ValueError):
                await self._send_json(send, 400, "Content-Length 无效")
                return
            if len(lengths) != 1 or next(iter(lengths)) < 0:
                await self._send_json(send, 400, "Content-Length 无效")
                return
            if next(iter(lengths)) > limit:
                await self._send_json(send, 413, f"批量请求体超过上限 {limit} bytes")
                return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if response_started:
                raise
            await self._send_json(
                send,
                413,
                f"批量请求体超过上限 {limit} bytes",
            )
