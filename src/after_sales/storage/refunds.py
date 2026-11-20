"""退款操作持久化视图。"""

import json


def operation_view(row):
    if row is None:
        return None
    result = dict(row)
    result["snapshot"] = json.loads(result.pop("snapshot_json"))
    result["result"] = json.loads(result.pop("result_json"))
    return result


def read_refund(connection, request_id):
    return operation_view(connection.execute("SELECT * FROM refund_operations WHERE request_id = ?", (request_id,)).fetchone())


def save_simulated_result(connection, operation_id, result, updated_by=None):
    connection.execute(
        """INSERT INTO simulated_refund_results (operation_id, result_json, updated_by) VALUES (?, ?, ?)
           ON CONFLICT(operation_id) DO UPDATE SET result_json = excluded.result_json,
           updated_by = excluded.updated_by, updated_at = CURRENT_TIMESTAMP""",
        (operation_id, json.dumps(result, ensure_ascii=False), updated_by),
    )


def query_simulated_result(connection, operation_id):
    row = connection.execute("SELECT result_json FROM simulated_refund_results WHERE operation_id = ?", (operation_id,)).fetchone()
    return json.loads(row["result_json"]) if row else {"status": "unknown", "simulated": True}
