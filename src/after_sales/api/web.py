"""静态网页和当前演示身份；业务接口仍分别校验客户、客服及仓库权限。"""

from fastapi import APIRouter
from fastapi.responses import FileResponse

from after_sales.api.customer import Service, Session
from after_sales.config import PROJECT_ROOT
from after_sales.domain.customer_service import InvalidSession
from after_sales.storage.database import connect_database


router = APIRouter()
WEB_ROOT = PROJECT_ROOT / "web"


@router.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@router.get("/demo/sessions/current")
def current_demo_session(service: Service, session_id: Session) -> dict:
    with connect_database(service.db_path) as connection:
        row = connection.execute(
            """SELECT u.id, u.display_name, u.role FROM sessions s
               JOIN users u ON u.id = s.user_id WHERE s.id = ?
               AND u.role IN ('customer', 'support', 'warehouse')""", (session_id,),
        ).fetchone()
    if row is None:
        raise InvalidSession("会话无效，请重新选择演示身份。")
    return {"user": dict(row)}
