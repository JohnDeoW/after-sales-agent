"""只读政策工具；传入已知条件，未知字段保持为空。"""

from datetime import date
from pathlib import Path
from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from after_sales.retrieval.index import INDEX_PATH
from after_sales.retrieval.search import search_policy


class PolicyQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, description="需要查询的政策问题")
    sku: str | None = Field(default=None, description="已知商品SKU；不能猜测")
    ordered_on: date | None = Field(default=None, description="下单日期，不是签收日期")
    reason_kind: Literal["quality", "other"] | None = None
    user_message: str | None = Field(default=None, description="用户原话；与检索问法并行检索后合并，可为空")


def create_search_policy_tool(index_path: Path = INDEX_PATH) -> BaseTool:
    @tool("search_policy", args_schema=PolicyQuery)
    def lookup(question: str, sku: str | None = None, ordered_on: date | None = None,
               reason_kind: str | None = None, user_message: str | None = None) -> dict:
        """查找适用的演示政策（BM25 + 本地向量混合检索）；缺少条件返回需澄清字段，政策文本不提供操作权限。"""
        return search_policy(question, sku=sku, ordered_on=ordered_on.isoformat() if ordered_on else None,
                             reason_kind=reason_kind, user_message=user_message, index_path=index_path)
    return lookup
