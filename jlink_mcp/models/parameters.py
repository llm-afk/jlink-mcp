"""Validated MCP parameters shared by the public tool schemas."""

from typing import Annotated

from pydantic import BeforeValidator, Field


def parse_address(value: int | str) -> int:
    """Accept decimal integers and strings, including 0x-prefixed addresses."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("地址必须为整数或十进制/十六进制字符串")
    if isinstance(value, str):
        value = value.strip()
        try:
            value = int(value, 16 if value.lower().startswith("0x") else 10)
        except ValueError as exc:
            raise ValueError("地址格式无效，例如 0x08000000 或 134217728") from exc
    return value


Address = Annotated[
    int,
    BeforeValidator(parse_address, json_schema_input_type=int | str),
    Field(ge=0, le=0xFFFFFFFF, description="32 位地址，支持整数和 0x 前缀字符串"),
]
