"""API 服务入口；发送消息时调用模型识别售后诉求。"""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
from dataclasses import replace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from after_sales.config import load_settings
from after_sales.observability import setup_tracing, flush as flush_traces
from after_sales.api.customer import router
from after_sales.api.support import router as support_router
from after_sales.domain.approvals import SupportRequired
from after_sales.api.return_events import router as return_event_router
from after_sales.api.exchanges import router as exchange_router
from after_sales.api.web import router as web_router, WEB_ROOT
from after_sales.domain.return_events import WarehouseRequired
from after_sales.agent.intake import RequestExtractor, IntakeUnavailable
from after_sales.agent.policy import PolicyExplainer
from after_sales.agent.llm import create_model
from after_sales.agent.query_agent import RuleToolSelector
from after_sales.retrieval.index import INDEX_PATH
from after_sales.domain.customer_service import CustomerService, InvalidCustomer, InvalidSession
from after_sales.workflow.graph import AfterSalesWorkflow
from after_sales.workflow.runtime import WorkflowStopped, Limits
from after_sales.domain.proposals import ProposalNotFound, ProposalConflict
from after_sales.storage.database import DEMO_DB_PATH, initialize_session_storage


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.settings = load_settings()
    app.state.phoenix_url = setup_tracing(app.state.settings)
    app.state.customer_service.confirmation_ttl_seconds = app.state.settings.confirmation_ttl_seconds
    app.state.customer_service.approval_ttl_seconds = app.state.settings.approval_ttl_seconds
    initialize_session_storage(app.state.customer_service.db_path)
    extractor = app.state.extractor or RequestExtractor(app.state.settings)
    explainer = app.state.policy_explainer or PolicyExplainer(app.state.settings)
    # 注入替身识别器（测试与离线回放）时，查询工具也由规则替身选择，不调用真实模型。
    selector = app.state.tool_selector or (RuleToolSelector() if app.state.extractor is not None else create_model(app.state.settings))
    limits = Limits()
    # 一轮最多两次模型调用；随单次超时同步预算，另留业务处理时间。
    limits = replace(limits, turn_seconds=limits.max_model_calls * app.state.settings.llm_timeout_seconds + 15)
    app.state.workflow = AfterSalesWorkflow(app.state.customer_service, extractor,
                                           limits=limits, policy_explainer=explainer, policy_index_path=app.state.policy_index_path,
                                           tool_selector=selector)
    yield
    app.state.workflow.close()
    flush_traces()


def create_app(db_path: Path = DEMO_DB_PATH, *, extractor=None, policy_explainer=None,
               policy_index_path: Path = INDEX_PATH, tool_selector=None) -> FastAPI:
    application = FastAPI(title="商品售后服务 Agent", lifespan=lifespan)
    application.state.customer_service = CustomerService(db_path)
    application.state.extractor = extractor
    application.state.policy_explainer = policy_explainer
    application.state.policy_index_path = policy_index_path
    application.state.tool_selector = tool_selector
    application.include_router(router)
    application.include_router(support_router)
    application.include_router(return_event_router)
    application.include_router(exchange_router)
    application.include_router(web_router)
    application.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")

    @application.middleware("http")
    async def revalidate_static(request, call_next):
        # 页面脚本更新后浏览器必须重新校验，避免新旧模块混用导致页面空白。
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @application.exception_handler(SupportRequired)
    @application.exception_handler(WarehouseRequired)
    async def support_required_handler(request, exc: SupportRequired) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @application.exception_handler(InvalidSession)
    async def invalid_session_handler(request, exc: InvalidSession) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=401)

    @application.exception_handler(InvalidCustomer)
    async def invalid_customer_handler(request, exc: InvalidCustomer) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @application.exception_handler(IntakeUnavailable)
    async def intake_unavailable_handler(request, exc: IntakeUnavailable) -> JSONResponse:
        # 超时 504、限流 429，其余模型故障 502；分类详情同时写入轨迹 error_detail。
        return JSONResponse(error_body(exc), status_code=exc.status_code)

    @application.exception_handler(ProposalNotFound)
    async def proposal_not_found_handler(request, exc: ProposalNotFound) -> JSONResponse:
        return JSONResponse(error_body(exc), status_code=404)

    @application.exception_handler(ProposalConflict)
    async def proposal_conflict_handler(request, exc: ProposalConflict) -> JSONResponse:
        return JSONResponse(error_body(exc), status_code=409)

    @application.exception_handler(WorkflowStopped)
    async def workflow_stopped_handler(request, exc: WorkflowStopped) -> JSONResponse:
        return JSONResponse(error_body(exc), status_code=exc.status_code)

    application.add_api_route("/health", health, methods=["GET"])

    @application.get("/observability")
    def observability(request: Request) -> dict:
        """页面用：Phoenix 已开启时返回本地调用链页面地址，否则为 null。"""
        return {"phoenix_url": getattr(request.app.state, "phoenix_url", None)}
    return application


def error_body(exc) -> dict:
    body = {"detail": str(exc)}
    if hasattr(exc, "workflow"):
        body["workflow"] = exc.workflow
    return body


async def health() -> dict[str, str]:
    """只表示服务存活，不表示中转站或模型已经接通。"""
    return {"status": "ok", "service": "after-sales-agent"}


app = create_app()


def run() -> None:
    uvicorn.run("after_sales.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    run()
