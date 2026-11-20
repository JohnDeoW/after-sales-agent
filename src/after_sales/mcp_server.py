"""只读工具 MCP 服务：把只读工具白名单以 MCP 协议提供（stdio 与 Streamable HTTP）。

- 每个 MCP 工具直接调用白名单中的同名 LangChain 工具，复用相同的业务查询与身份校验，不复制业务逻辑；
- 会话身份不是工具参数：HTTP 方式取请求头 X-After-Sales-Session，stdio 方式取启动参数 --session；
  缺少身份时拒绝查询（MissingSession），身份对应的用户与归属仍由业务服务校验；
- 只提供只读工具；确认、提交、退款、审批等写操作不通过 MCP 开放。
"""

import argparse
import inspect
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from after_sales.domain.customer_service import CustomerService, InvalidSession
from after_sales.domain.proposals import ProposalNotFound
from after_sales.storage.database import DEMO_DB_PATH
from after_sales.tools.registry import READ_ONLY_TOOLS
from after_sales.tools.scope import MissingSession, tool_config

SESSION_HEADER = "x-after-sales-session"


def _mcp_function(tool, customers: CustomerService, default_session: str | None):
    """按白名单工具的参数 schema 生成 MCP 工具函数（参数与类型一致，另加 MCP 注入的 Context）。"""
    fields = tool.args_schema.model_fields

    def call(ctx: Context, **args) -> dict:
        request = ctx.request_context.request  # HTTP 方式为 Starlette 请求；stdio 方式为 None
        session_id = (request.headers.get(SESSION_HEADER) if request is not None else None) or default_session
        try:
            if not session_id:
                raise MissingSession("缺少服务端会话身份，拒绝查询。")
            # 结果统一放在 data 中：对象与列表都能作为结构化结果返回。
            return {"data": tool.invoke(args, tool_config(customers, session_id))}
        except (MissingSession, InvalidSession, ProposalNotFound) as exc:
            # 预期内的拒绝原样告知客户端；其他异常只返回通用错误，不泄露内部信息。
            raise ToolError(str(exc)) from None

    parameters = [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=field.annotation)
                  for name, field in fields.items()]
    parameters.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context))
    call.__signature__ = inspect.Signature(parameters, return_annotation=dict)
    call.__annotations__ = {**{name: field.annotation for name, field in fields.items()}, "ctx": Context, "return": dict}
    call.__name__ = tool.name
    return call


def create_mcp_server(db_path: Path = DEMO_DB_PATH, *, session_id: str | None = None, port: int = 8765) -> FastMCP:
    customers = CustomerService(db_path)
    # HTTP 方式：无状态、JSON 响应；挂载到主服务时路径为 /mcp/，单独运行时为 /mcp。
    server = FastMCP("after-sales-readonly", instructions="商品售后演示系统的只读查询工具；只返回当前会话用户本人的记录。",
                     stateless_http=True, json_response=True, port=port)
    for tool in READ_ONLY_TOOLS.values():
        server.add_tool(_mcp_function(tool, customers, session_id), name=tool.name, description=tool.description)
    return server


def http_app(server: FastMCP):
    """挂载到主服务用的 Streamable HTTP 应用（路径 /）。"""
    server.settings.streamable_http_path = "/"
    return server.streamable_http_app()


def run() -> None:
    parser = argparse.ArgumentParser(description="以 MCP 协议提供售后只读查询工具。")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--db", type=Path, default=DEMO_DB_PATH, help="业务库路径，默认 data/demo.sqlite3")
    parser.add_argument("--session", help="stdio 方式使用的服务端会话编号（HTTP 方式改用请求头 X-After-Sales-Session）")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_mcp_server(args.db.resolve(), session_id=args.session if args.transport == "stdio" else None,
                               port=args.port)
    server.run("stdio" if args.transport == "stdio" else "streamable-http")


if __name__ == "__main__":
    run()
