"""静态网页和当前演示身份；业务接口仍分别校验客户、客服及仓库权限。"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from after_sales.api.customer import Service, get_session_id
from after_sales.config import PROJECT_ROOT, business_today
from after_sales.domain.customer_service import InvalidSession
from after_sales.storage.database import connect_database
from after_sales.storage.seed import reset_demo_in_place


router = APIRouter()
WEB_ROOT = PROJECT_ROOT / "web"


@router.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@router.get("/demo/sessions/current")
def current_demo_session(request: Request, service: Service) -> dict:
    """页面启动时探测当前标签页的身份；没有或已失效的会话返回 user=null（不是错误），由页面显示身份选择。"""
    try:
        session_id = get_session_id(request)
    except InvalidSession:
        return {"user": None}
    with connect_database(service.db_path) as connection:
        row = connection.execute(
            """SELECT u.id, u.display_name, u.role FROM sessions s
               JOIN users u ON u.id = s.user_id WHERE s.id = ?
               AND u.role IN ('customer', 'support', 'warehouse')""", (session_id,),
        ).fetchone()
    return {"user": dict(row) if row else None}


@router.post("/demo/reset")
def reset_demo(request: Request, service: Service) -> dict:
    """准备全新演示数据（仅限本机访问）：清空全部案件、退款、会话与对话记录，按今天重新生成演示订单。"""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "只能在运行服务的本机上重置演示数据。")
    as_of = business_today()
    reset_demo_in_place(service.db_path, as_of)
    # 检查点表随业务表一起删除；让检查点存储在下次使用时重新建表。
    request.app.state.workflow.checkpointer.is_setup = False
    return {"as_of": as_of.isoformat()}
