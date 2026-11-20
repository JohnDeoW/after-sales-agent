"""按当前会话保存和读取运行轨迹。"""

import json

from after_sales.domain.customer_service import _session_user
from after_sales.domain.proposals import ProposalNotFound
from after_sales.storage.database import connect_database


def save_trace(path, session_id: str, trace: dict) -> None:
    with connect_database(path) as connection:
        connection.execute("INSERT INTO workflow_runs (id, session_id, trace_json) VALUES (?, ?, ?)",
                           (trace["id"], session_id, json.dumps(trace, ensure_ascii=False)))


def get_traces(path, session_id: str, run_id: str | None = None):
    with connect_database(path) as connection:
        _session_user(connection, session_id)
        if run_id:
            row = connection.execute("SELECT trace_json FROM workflow_runs WHERE id = ? AND session_id = ?",
                                     (run_id, session_id)).fetchone()
            if row is None:
                raise ProposalNotFound("未找到当前会话的运行轨迹。")
            return json.loads(row["trace_json"])
        rows = connection.execute(
            "SELECT trace_json FROM workflow_runs WHERE session_id = ? ORDER BY rowid DESC LIMIT 20", (session_id,))
        return [json.loads(row["trace_json"]) for row in rows]
