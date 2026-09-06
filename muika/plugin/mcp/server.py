import asyncio
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Optional

from httpx import AsyncClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .config import mcpConfig


class Tool:
    """
    MCP Tool
    """

    def __init__(self, name: str, description: str, input_schema: dict[str, Any]) -> None:
        self.name: str = name
        self.description: str = description
        self.input_schema: dict[str, Any] = input_schema

    def format_for_llm(self) -> str:
        """
        为 llm 生成工具描述

        :return: 工具描述
        """
        args_desc = []
        if "properties" in self.input_schema:
            for param_name, param_info in self.input_schema["properties"].items():
                arg_desc = f"- {param_name}: {param_info.get('description', 'No description')}"
                if param_name in self.input_schema.get("required", []):
                    arg_desc += " (required)"
                args_desc.append(arg_desc)

        return f"Tool: {self.name}\n" f"Description: {self.description}\n" f"Arguments:{chr(10).join(args_desc)}" ""


class Server:
    """
    管理 MCP 服务器连接和工具执行的 Server 实例
    """

    def __init__(self, name: str, config: mcpConfig) -> None:
        self.name: str = name
        self.config: mcpConfig = config
        self.session: ClientSession | None = None
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.exit_stack: AsyncExitStack = AsyncExitStack()
        self._transport_initializers = {
            "stdio": self._initialize_stdio,
            "sse": self._initialize_sse,
            "streamable_http": self._initialize_streamable_http,
        }

    async def _initialize_stdio(self) -> tuple[Any, Any]:
        """
        初始化 stdio 传输方式

        :return: (read, write) 元组
        """
        server_params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env={**os.environ, **self.config.env} if self.config.env else None,
        )
        transport_context = await self.exit_stack.enter_async_context(stdio_client(server_params))
        return transport_context

    async def _initialize_sse(self) -> tuple[Any, Any]:
        """
        初始化 sse 传输方式

        :return: (read, write) 元组
        """
        transport_context = await self.exit_stack.enter_async_context(
            sse_client(self.config.url, headers=self.config.headers)
        )
        return transport_context

    async def _initialize_streamable_http(self) -> tuple[Any, Any]:
        """
        初始化 streamable_http 传输方式

        :return: (read, write) 元组
        """
        client = AsyncClient(headers=self.config.headers)
        read, write, *_ = await self.exit_stack.enter_async_context(
            streamable_http_client(self.config.url, http_client=client)
        )
        return read, write

    async def initialize(self) -> None:
        """
        初始化实例
        """
        transport = self.config.type
        initializer = self._transport_initializers[transport]
        read, write = await initializer()
        session = await self.exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self.session = session

    async def list_tools(self) -> list[Tool]:
        """
        从 MCP 服务器获得可用工具列表

        :return: 工具列表

        :raises RuntimeError: 如果服务器未启动
        """
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")

        tools_response = await self.session.list_tools()
        tools: list[Tool] = []

        for item in tools_response:
            if isinstance(item, tuple) and item[0] == "tools":
                tools.extend(Tool(tool.name, tool.description, tool.inputSchema) for tool in item[1])

        return tools

    async def execute_tool(self, tool_name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        """执行一次 MCP 工具；传输失败时由调用者核对结果。"""
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")
        logging.info(f"Executing {tool_name}...")
        return await self.session.call_tool(tool_name, arguments)

    async def cleanup(self) -> None:
        """Clean up server resources."""
        async with self._cleanup_lock:
            try:
                await self.exit_stack.aclose()
                self.session = None
            except Exception as e:
                logging.error(f"Error during cleanup of server {self.name}: {e}")
