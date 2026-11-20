"""统一模型连通验证入口；仅显式运行时发起真实 API 请求。"""

import argparse
import json
import re
import sys

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from openai import APIError

from after_sales.agent.llm import create_model
from after_sales.config import ConfigurationError
from after_sales.tools.calculator import calculator


def check_text(model: ChatOpenAI) -> None:
    reply = model.invoke("请用一句中文向准备申请售后的用户问好。")
    if not reply.text.strip():
        raise RuntimeError("普通请求没有返回文本。")
    print(f"[普通回复通过] {reply.text}", flush=True)


def check_stream(model: ChatOpenAI) -> None:
    print("[流式回复] ", end="", flush=True)
    text_chunks = 0
    for chunk in model.stream("请用两句中文说明，申请售后时应准备订单号和问题描述。"):
        if chunk.text:
            text_chunks += 1
            print(chunk.text, end="", flush=True)
    print(flush=True)
    if not text_chunks:
        raise RuntimeError("流式请求没有返回文本片段。")
    print(f"[流式输出通过] 收到 {text_chunks} 个文本片段。", flush=True)


def check_tool(model: ChatOpenAI) -> None:
    messages = [HumanMessage("请调用 calculator 计算 23 乘以 19，收到工具结果后用中文回答。")]
    model_with_tools = model.bind_tools([calculator], strict=True)
    reply = model_with_tools.invoke(messages)
    if len(reply.tool_calls) != 1 or reply.tool_calls[0]["name"] != "calculator":
        raise RuntimeError("模型未按本次样例要求调用一次 calculator。")

    call = reply.tool_calls[0]
    args = call["args"]
    if args.get("operation") != "multiply" or sorted([args.get("a", 0), args.get("b", 0)]) != [19, 23]:
        raise RuntimeError("计算器调用参数与 23 乘以 19 不一致。")
    # 传入完整 ToolCall，LangChain 会生成携带相同 tool_call_id 的 ToolMessage。
    result = calculator.invoke(call)
    if result.content != "437" or result.tool_call_id != call["id"]:
        raise RuntimeError("计算器结果或工具调用标识不正确。")
    print(f"[工具调用] calculator {json.dumps(args, ensure_ascii=False)} -> {result.content}", flush=True)

    # 保留完整模型消息（含 reasoning 项）和工具结果，回传给同一个模型。
    final = model_with_tools.invoke([*messages, reply, result])
    if final.tool_calls or not re.search(r"\b437\b", final.text):
        raise RuntimeError("工具结果已回传，但模型尚未给出包含正确结果的最终回答。")
    print(f"[工具回传通过] {final.text}", flush=True)


def run() -> None:
    parser = argparse.ArgumentParser(description="验证项目配置的真实模型接口；all 通常发起 4 次请求。")
    parser.add_argument("--mode", choices=("all", "text", "stream", "tool"), default="all")
    args = parser.parse_args()
    checks = {"text": check_text, "stream": check_stream, "tool": check_tool}
    stage = "配置"
    try:
        model = create_model()
        print(f"接口：Responses；模型：{model.model_name}", flush=True)
        for stage, check in checks.items():
            if args.mode in ("all", stage):
                check(model)
    except APIError as exc:
        # 不直接输出服务端错误体、请求头或完整异常，避免泄露密钥。
        status = getattr(exc, "status_code", None)
        print(f"[{stage} 失败] {type(exc).__name__}，HTTP 状态：{status or '无'}；未切换地址或模型。", file=sys.stderr)
        raise SystemExit(1) from None
    except (ConfigurationError, RuntimeError) as exc:
        print(f"[{stage} 失败] {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except ValueError:
        # LangChain 也会把 Responses 的流式错误事件转换为 ValueError。
        print(f"[{stage} 失败] 响应解析失败或接口返回流式错误；未输出原始错误体，未切换地址或模型。", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()
