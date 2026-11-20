"""从 LangGraph messages 流中提取政策答复草稿；只公开 PolicyAnswer.text，不传递工具 JSON 或推理内容。"""

from langchain_core.messages import AIMessageChunk

PREVIEW_TOOL = "PolicyAnswer"


class DraftPreview:
    def __init__(self):
        self._messages: dict = {}
        self._sent: dict = {}

    def feed(self, chunk, metadata=None) -> str:
        """合并同一条模型输出的分片，返回新增的答复文字（没有新增时为空字符串）。"""
        if not isinstance(chunk, AIMessageChunk):
            return ""
        key = chunk.id or "current"
        merged = self._messages[key] = chunk if key not in self._messages else self._messages[key] + chunk
        call = next((c for c in merged.tool_calls if c.get("name") == PREVIEW_TOOL), None)
        text = (call or {}).get("args", {}).get("text") if call else None
        previous = self._sent.get(key, "")
        if not isinstance(text, str) or not text.startswith(previous) or text == previous:
            return ""
        self._sent[key] = text
        return text[len(previous):]
