"""Agent 经 MCP 调用只读工具：langchain-mcp-adapters 的 MultiServerMCPClient 连接只读工具 MCP 服务。

- 工具定义启动后首次使用时加载一次并缓存；
- 会话身份由工具拦截器在每次调用时写入请求头 X-After-Sales-Session（取自运行配置），不是模型参数；
- MCP 服务不可用时抛出 MCPUnavailable 明确提示，不退回本地工具，也不放宽身份校验。
"""

import json
from contextvars import ContextVar

from langchain_core.tools import ToolException
from langchain_mcp_adapters.client import MultiServerMCPClient

from after_sales.tools.scope import MissingSession

SESSION_HEADER = "X-After-Sales-Session"
SERVER = "after_sales"
_session: ContextVar[str | None] = ContextVar("after_sales_mcp_session", default=None)


class MCPUnavailable(RuntimeError):
    """无法连接只读工具 MCP 服务；本轮未完成，不改用其他查询方式。"""
    status_code = 503


class SessionHeader:
    """工具拦截器：把当前调用的会话身份放进 MCP 请求头。"""

    async def __call__(self, request, handler):
        session_id = _session.get()
        if not session_id:
            raise MissingSession("缺少服务端会话身份，拒绝查询。")
        return await handler(request.override(headers={SESSION_HEADER: session_id}))


class MCPToolProvider:
    def __init__(self, url: str):
        self.url = url
        self.client = MultiServerMCPClient({SERVER: {"transport": "streamable_http", "url": url}},
                                          tool_interceptors=[SessionHeader()])
        self._tools: dict | None = None

    async def _load(self) -> dict:
        if self._tools is None:
            try:
                self._tools = {tool.name: tool for tool in await self.client.get_tools()}
            except Exception:
                raise MCPUnavailable(f"无法连接只读工具 MCP 服务（{self.url}），本轮查询未完成；请确认服务已启动。") from None
        return self._tools

    async def call(self, name: str, args: dict, session_id: str):
        """以指定会话身份调用 MCP 工具，返回与直接调用相同的业务数据。"""
        tools = await self._load()
        token = _session.set(session_id)
        try:
            message = await tools[name].ainvoke({"type": "tool_call", "name": name, "args": args, "id": f"mcp-{name}"})
        except MissingSession:
            raise
        except Exception:
            raise MCPUnavailable(f"调用只读工具 MCP 服务失败（{self.url}），本轮查询未完成；请确认服务状态后重试。") from None
        finally:
            _session.reset(token)
        text = _text(message.content)
        if message.status == "error":
            raise ToolException("查询失败：" + text)
        structured = (message.artifact or {}).get("structured_content") if isinstance(message.artifact, dict) else None
        return (structured or json.loads(text))["data"]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") if isinstance(block, dict) else str(block) for block in content)
