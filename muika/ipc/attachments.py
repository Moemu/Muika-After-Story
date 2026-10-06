"""传输聊天附件，不提供任意文件或任务目录访问。"""

import asyncio
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import aiohttp
from aiohttp import web

from muika.models import Resource

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def _save_attachment(path: Path, content: bytes | bytearray) -> None:
    """原子替换附件，并在写入失败或请求取消后清理临时文件。"""
    temporary = path.with_suffix(f".{uuid4().hex}.upload")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def attachment_routes(directory: Path, secret: str) -> list[web.RouteDef]:
    """提供认证后的聊天附件上传和下载，文件名由内容哈希确定。"""

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.headers.get("X-Auth-Token") != secret:
            raise web.HTTPUnauthorized()
        key = request.match_info["key"]
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise web.HTTPBadRequest(text="Invalid attachment hash")
        path = directory / key
        if request.method == "PUT":
            content = bytearray()
            async for chunk in request.content.iter_chunked(65536):
                content.extend(chunk)
                if len(content) > MAX_ATTACHMENT_BYTES:
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_ATTACHMENT_BYTES, actual_size=len(content))
            if hashlib.sha256(content).hexdigest() != key:
                raise web.HTTPBadRequest(text="Attachment hash does not match")
            directory.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(_save_attachment, path, content)
            return web.Response(status=201)
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    return [web.put("/attachments/{key}", handle), web.get("/attachments/{key}", handle)]


class AttachmentTransfer:
    """只向配置入口发送认证信息，下载前检查内容和大小。"""

    def __init__(self, endpoint: str, secret: str, directory: Path) -> None:
        parsed = urlsplit(endpoint)
        self.base = urlunsplit(("https" if parsed.scheme in {"wss", "https"} else "http", parsed.netloc, "", "", ""))
        self.secret, self.directory = secret, directory

    async def upload(self, resource: Resource) -> dict:
        if not resource.path and resource.raw is None and resource.url and resource.url.startswith("/attachments/"):
            return {**resource.to_dict(), "path": ""}
        if resource.path:
            content = await asyncio.to_thread(Path(resource.path).read_bytes)
        elif isinstance(resource.raw, bytes):
            content = resource.raw
        elif resource.raw is not None:
            content = resource.raw.getvalue()
        else:
            raise ValueError("A chat attachment needs local content")
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Chat attachment exceeds 20 MiB")
        url = "/attachments/" + hashlib.sha256(content).hexdigest()
        async with aiohttp.ClientSession(headers={"X-Auth-Token": self.secret}) as session:
            async with session.put(self.base + url, data=content) as response:
                response.raise_for_status()
        return {**resource.to_dict(), "path": "", "url": url}

    async def download(self, resource: Resource) -> Resource:
        if not resource.url or not resource.url.startswith("/attachments/"):
            raise ValueError("Attachment does not belong to the configured chat endpoint")
        key = resource.url.removeprefix("/attachments/")
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("Invalid attachment hash")
        target = self.directory / (key + (resource.extension or ".bin"))
        if not target.is_file():
            async with aiohttp.ClientSession(headers={"X-Auth-Token": self.secret}) as session:
                async with session.get(self.base + resource.url) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        content.extend(chunk)
                        if len(content) > MAX_ATTACHMENT_BYTES:
                            raise ValueError("Chat attachment exceeds 20 MiB")
            if hashlib.sha256(content).hexdigest() != key:
                raise ValueError("Downloaded attachment hash does not match")
            self.directory.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_bytes, content)
        return Resource(resource.type, path=str(target), url=resource.url, mimetype=resource.mimetype)
