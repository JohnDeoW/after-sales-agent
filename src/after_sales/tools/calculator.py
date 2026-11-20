"""用于验证模型工具调用的整数计算器，不承担退款业务规则。"""

from typing import Literal

from langchain_core.tools import tool


@tool
def calculator(operation: Literal["add", "subtract", "multiply"], a: int, b: int) -> int:
    """计算两个整数：add 为相加，subtract 为 a 减 b，multiply 为相乘。"""
    if operation == "add":
        return a + b
    if operation == "subtract":
        return a - b
    return a * b
