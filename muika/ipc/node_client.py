"""向固定入口建立出站连接，业务调用者决定断线后的恢复策略。"""

import asyncio
import hashlib
import ssl
from pathlib import Path
from types import TracebackType
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from muika.models import Resource
from muika.node.models import ResourceReference
from muika.node.resources import ResourceVault

from .node_protocol import (
    HEARTBEAT_INTERVAL_SECONDS,
    PROTOCOL_VERSION,
    NodeRequest,
    NodeResponse,
    RegisterNode,
)


class NodeRequestError(ValueError):
    """表示服务端明确拒绝请求。"""

    def __init__(self, message: str, *, code: Literal["checkpoint_unavailable"] | None = None) -> None:
        super().__init__(message)
        self.code = code


class NodeClient:
    """串行关联请求与响应，超时后关闭连接以免误用迟到响应。"""

    def __init__(self, address: str, token: str, *, ca_file: Path | None = None) -> None:
        url = urlsplit(address)
        if url.scheme not in {"ws", "wss", "http", "https"} or not url.hostname:
            raise ValueError("A WebSocket endpoint is required.")
        if url.scheme in {"ws", "http"} and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote node connections require TLS.")
        if not token:
            raise ValueError("A node token is required.")
        self.address = address
        self._token = token
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._lock = asyncio.Lock()
        self._ssl = ssl.create_default_context(cafile=str(ca_file)) if ca_file else None

    async def __aenter__(self) -> "NodeClient":
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=self._ssl or True),
            headers={
                "Authorization": f"Bearer {self._token}",
                "X-MAS-Protocol": str(PROTOCOL_VERSION),
            },
        )
        try:
            self._ws = await self._session.ws_connect(
                self.address,
                heartbeat=HEARTBEAT_INTERVAL_SECONDS,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "X-MAS-Protocol": str(PROTOCOL_VERSION),
                },
            )
            await self.request(RegisterNode())
        except BaseException:
            await self._session.close()
            self._session = None
            raise
        return self

    def resource_url(self, reference: ResourceReference) -> str:
        url = urlsplit(self.address)
        scheme = "https" if url.scheme in {"wss", "https"} else "http"
        return f"{scheme}://{url.netloc}/node/resources/{reference.sha256}"

    async def upload_resource(self, resource: Resource, vault: ResourceVault) -> ResourceReference:
        """上传不可变副本；重传沿用同一散列。"""
        if self._session is None:
            raise ConnectionError("Node client is not connected.")
        reference = vault.preserve(resource)
        async with self._session.put(self.resource_url(reference), data=vault.path(reference).read_bytes()) as response:
            response.raise_for_status()
        return reference

    async def download_resource(self, reference: ResourceReference, vault: ResourceVault) -> Resource:
        """核验接收副本后返回当前节点上的本地资源。"""
        path = vault.path(reference)
        if not path.is_file():
            if self._session is None:
                raise ConnectionError("Node client is not connected.")
            async with self._session.get(self.resource_url(reference)) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    content.extend(chunk)
                    if len(content) > reference.size:
                        raise ValueError("Downloaded resource exceeds its declared size.")
            temporary = path.parent / (uuid4().hex + ".download")
            try:
                temporary.write_bytes(content)
                # 验证完成后才发布可用副本。
                if len(content) != reference.size or hashlib.sha256(content).hexdigest() != reference.sha256:
                    raise ValueError("Downloaded resource does not match its reference.")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return vault.materialize(reference)

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """幂等关闭连接，不把断线当作业务执行失败。"""
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def request(self, request: NodeRequest) -> NodeResponse:
        """执行一个请求；传输失败时调用者必须按业务身份核对结果。"""
        async with self._lock:
            if self._ws is None:
                raise ConnectionError("Node client is not connected.")
            try:
                await self._ws.send_str(request.model_dump_json())
                frame = await self._ws.receive(timeout=30)
                if frame.type != aiohttp.WSMsgType.TEXT:
                    raise ConnectionError("State service connection closed.")
                response = NodeResponse.model_validate_json(frame.data)
                if response.request_id != request.request_id:
                    raise ConnectionError("State response does not match its request.")
            except BaseException:
                await self.close()
                raise
            if response.error is not None:
                raise NodeRequestError(response.error, code=response.error_code)
            return response
