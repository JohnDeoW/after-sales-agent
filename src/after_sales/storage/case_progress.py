"""案件检查点独立于浏览器会话；与业务写入共用事务。"""

import json


def backfill_progress(connection) -> None:
    """兼容已有案件，已有检查点不覆盖；不重置业务记录。"""
    rows = connection.execute(
        "SELECT * FROM cases WHERE id NOT IN (SELECT case_id FROM case_progress)"
    ).fetchall()
    for case in rows:
        context = connection.execute(
            """SELECT s.state_json FROM conversation_states s WHERE s.case_id = ?
               ORDER BY (SELECT MAX(m.id) FROM messages m WHERE m.session_id = s.session_id) DESC LIMIT 1""",
            (case["id"],),
        ).fetchone()
        if context:
            state = json.loads(context["state_json"])
        else:
            draft = {k: case[k] for k in ("order_id", "order_item_id", "quantity", "reason", "opened") if case[k] is not None}
            if draft.get("order_item_id"):
                draft["item"] = draft["order_item_id"]
            state = {"draft": draft, "last_intent": case["intent"], "query_order_id": None,
                     "cancelled": case["status"] == "cancelled"}
            proposal = connection.execute(
                "SELECT id FROM proposals WHERE case_id = ? ORDER BY version DESC LIMIT 1", (case["id"],)
            ).fetchone()
            if proposal:
                state["proposal_id"] = proposal["id"]
        draft = state["draft"]
        missing = []
        if case["status"] == "collecting_information":
            for key in ("order_id", "order_item_id", "quantity", "reason", "opened"):
                if key == "opened" and draft.get("reason_kind") == "quality":
                    continue
                if draft.get(key) is None:
                    missing = ["item" if key == "order_item_id" else key]
                    break
        connection.execute(
            "INSERT INTO case_progress (case_id, state_json, missing_fields_json) VALUES (?, ?, ?)",
            (case["id"], json.dumps(state, ensure_ascii=False), json.dumps(missing)),
        )


def save_state(connection, session_id, case_id, state, missing_fields=None) -> None:
    payload = json.dumps(state, ensure_ascii=False)
    previous = connection.execute("SELECT case_id FROM conversation_states WHERE session_id = ?", (session_id,)).fetchone()
    connection.execute(
        """INSERT INTO conversation_states (session_id, case_id, state_json) VALUES (?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET case_id = excluded.case_id, state_json = excluded.state_json""",
        (session_id, case_id, payload),
    )
    if case_id is not None and (previous is None or previous["case_id"] != case_id):
        connection.execute(
            """UPDATE conversation_states SET history_start_id =
               (SELECT COALESCE(MAX(id), 0) + 1 FROM messages WHERE session_id = ?) WHERE session_id = ?""",
            (session_id, session_id),
        )
    if case_id is None:
        return
    save_case_state(connection, case_id, state, missing_fields)


def save_case_state(connection, case_id, state, missing_fields=None):
    payload = json.dumps(state, ensure_ascii=False)
    connection.execute(
        """INSERT INTO case_progress (case_id, state_json, missing_fields_json) VALUES (?, ?, COALESCE(?, '[]'))
           ON CONFLICT(case_id) DO UPDATE SET state_json = excluded.state_json,
           missing_fields_json = COALESCE(?, case_progress.missing_fields_json), revision = revision + 1""",
        (case_id, payload, json.dumps(missing_fields) if missing_fields is not None else None,
         json.dumps(missing_fields) if missing_fields is not None else None),
    )
    # 多个会话打开同一案件时，都读取同一份最新草稿；旧的在途识别由 revision 拒绝。
    connection.execute("UPDATE conversation_states SET state_json = ? WHERE case_id = ?", (payload, case_id))
