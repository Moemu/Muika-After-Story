import asyncio
from typing import Any, Optional

from muika.utils.logger import logger

from .config import get_mcp_server_config
from .server import Server, Tool

_servers: list[Server] = []
_tools: list[dict[str, dict]] = []


async def initialize_servers() -> None:
    """
    初始化全部 MCP 实例
    """
    if _servers:
        return
    server_config = get_mcp_server_config()
    _servers.extend([Server(name, srv_config) for name, srv_config in server_config.items()])
    for server in _servers:
        logger.info(f"Initializing MCP server: {server.name}")
        try:
            await server.initialize()
            _tools.extend(transform_json(tool) for tool in await server.list_tools())
        except Exception as e:
            logger.error(f"MCP server initialization failed: {e}")
            await cleanup_servers()
            raise


async def handle_mcp_tool(tool: str, arguments: Optional[dict[str, Any]] = None) -> Optional[str]:
    """
    处理 MCP Tool 调用
    """
    logger.info(f"执行 MCP 工具: {tool} (参数: {arguments})")

    for server in _servers:
        server_tools = await server.list_tools()
        if not any(server_tool.name == tool for server_tool in server_tools):
            continue

        try:
            result = await server.execute_tool(tool, arguments)

            if isinstance(result, dict) and "progress" in result:
                progress = result["progress"]
                total = result["total"]
                percentage = (progress / total) * 100
                logger.info(f"工具执行进度: {progress}/{total} ({percentage:.1f}%)")

            return f"Tool execution result: {result}"
        except Exception as e:
            error_msg = f"Error executing tool: {str(e)}"
            logger.error(error_msg)
            return error_msg

    return None  # Not found.


async def cleanup_servers() -> None:
    """
    清理 MCP 实例
    """
    servers = list(_servers)
    _servers.clear()
    _tools.clear()
    results = await asyncio.gather(*(server.cleanup() for server in servers), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            logger.warning(f"MCP server cleanup failed: {result}")


def transform_json(tool: Tool) -> dict[str, Any]:
    """
    将 MCP Tool 转换为 OpenAI 所需的 parameters 格式，并删除多余字段
    """
    func_desc = {"name": tool.name, "description": tool.description, "parameters": {}, "required": []}

    if tool.input_schema:
        parameters = {
            "type": tool.input_schema.get("type", "object"),
            "properties": tool.input_schema.get("properties", {}),
            "required": tool.input_schema.get("required", []),
        }
        func_desc["parameters"] = parameters

    output = {"type": "function", "function": func_desc}

    return output


def get_mcp_list() -> list[dict[str, dict]]:
    """返回初始化时获取的 MCP 工具列表副本。"""
    return list(_tools)
