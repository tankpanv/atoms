"""Streaming HTTP and WebSocket forwarding between the gateway and agent services."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from websockets.asyncio.client import connect as websocket_connect


HOP_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "cookie", "authorization", "origin", "x-agent-secret", "x-worker-token"}


async def forward_http(request: Request, target: str, headers: dict[str, str], *, project_authorization: str = ""):
    if request.url.query:
        target += "?" + request.url.query
    forwarded = {key: value for key, value in request.headers.items() if key.lower() not in HOP_HEADERS}
    forwarded.update(headers)
    if project_authorization:
        forwarded["authorization"] = project_authorization
    client = httpx.AsyncClient(timeout=httpx.Timeout(60, read=None), follow_redirects=False)
    try:
        upstream = await client.send(client.build_request(request.method, target, headers=forwarded, content=await request.body()), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"Agent service unavailable: {exc}") from exc
    response_headers = {key: value for key, value in upstream.headers.items()
                        if key.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection", "set-cookie"}}

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=response_headers)


async def forward_websocket(websocket: WebSocket, target: str, headers: dict[str, str]):
    if websocket.url.query:
        target += "?" + websocket.url.query
    try:
        protocols = websocket.scope.get("subprotocols") or []
        async with websocket_connect(target, max_size=None, subprotocols=protocols, additional_headers=headers) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def to_upstream():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    await upstream.send(message.get("text") if message.get("text") is not None else message.get("bytes", b""))

            async def to_browser():
                async for message in upstream:
                    if isinstance(message, str):
                        await websocket.send_text(message)
                    else:
                        await websocket.send_bytes(message)

            tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_browser())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled():
                    task.exception()
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
