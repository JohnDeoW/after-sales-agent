"""退回事件的持久化视图，不依赖对话或模型推断收货与检验。"""

import json


def event_view(row):
    event = dict(row)
    event["payload"] = json.loads(event.pop("payload_json"))
    return event


def read_events(connection, request_id):
    return [event_view(row) for row in connection.execute(
        "SELECT * FROM return_events WHERE request_id = ? ORDER BY rowid", (request_id,))]


def return_conditions_met(events):
    by_kind = {event["kind"]: event["payload"] for event in events}
    return ("shipment" in by_kind and "receipt" in by_kind
            and by_kind.get("inspection", {}).get("result") == "passed")
