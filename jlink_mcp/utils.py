"""JLink MCP 工具函数."""

import logging
import os
from typing import Optional


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """配置日志记录器.

    Args:
        level: 日志级别，默认为 INFO

    Returns:
        配置好的 Logger 实例
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    return logging.getLogger("jlink_mcp")


def format_bytes(data: bytes, width: int = 16) -> str:
    """格式化字节数据为十六进制字符串.

    Args:
        data: 字节数据
        width: 每行显示的字节数

    Returns:
        格式化后的字符串
    """
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:08x}:  {hex_part:<{width * 3}}  {ascii_part}")
    return "\n".join(lines)


def parse_hex_string(hex_str: str) -> bytes:
    """解析十六进制字符串为字节.

    Args:
        hex_str: 十六进制字符串（如 "DEADBEEF" 或 "0xDE 0xAD"）

    Returns:
        解析后的字节数据

    Raises:
        ValueError: 如果解析失败
    """
    # 移除空格、0x 前缀等
    cleaned = hex_str.replace(" ", "").replace("0x", "").replace("0X", "")
    if len(cleaned) % 2 != 0:
        cleaned = "0" + cleaned
    try:
        return bytes.fromhex(cleaned)
    except ValueError as e:
        raise ValueError(f"无效的十六进制字符串: {hex_str}") from e


def validate_address(address: int, size: int = 4) -> None:
    """验证地址是否有效.

    Args:
        address: 内存地址
        size: 访问大小（字节）

    Raises:
        ValueError: 如果地址无效
    """
    if not isinstance(address, int) or isinstance(address, bool):
        raise ValueError("地址必须是整数")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("访问大小必须是正整数")
    if address < 0:
        raise ValueError(f"地址不能为负数: {address}")
    if address % size != 0:
        raise ValueError(f"地址 {address:#x} 未按 {size} 字节对齐")


def human_readable_size(size_bytes: int) -> str:
    """将字节大小转换为人类可读格式.

    Args:
        size_bytes: 字节数

    Returns:
        人类可读字符串（如 "64 KB"）
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def truncate_string(s: str, max_length: int = 100, suffix: str = "...") -> str:
    """截断长字符串.

    Args:
        s: 原始字符串
        max_length: 最大长度
        suffix: 截断后添加的后缀

    Returns:
        截断后的字符串
    """
    if len(s) <= max_length:
        return s
    return s[:max_length - len(suffix)] + suffix


def find_jlink_dll() -> Optional[str]:
    """自动查找 JLink_x64.dll 的完整路径.

    查找顺序：
    1. ``JLINK_LIB_PATH`` 环境变量（目录或 DLL 完整路径）
    2. 常见安装目录（C/D/E 盘的 SEGGER 目录下 JLink* / JLink_Vxxx 子目录）
    3. 系统 ``PATH`` 中的目录

    Returns:
        DLL 完整路径；未找到则返回 None，调用方应回退到 pylink 默认查找。

    目的：用户把 J-Link 软件装在非默认位置（如 D 盘的 JLink_V942）时，
    无需手动设置 ``JLINK_LIB_PATH`` 也能自动定位 DLL，开箱即用。
    """
    # 1. 环境变量（目录或文件）
    lib_path_env = os.environ.get("JLINK_LIB_PATH")
    if lib_path_env:
        if os.path.isdir(lib_path_env):
            candidate = os.path.join(lib_path_env, "JLink_x64.dll")
            if os.path.isfile(candidate):
                return candidate
        elif os.path.isfile(lib_path_env):
            return lib_path_env

    # 2. 常见安装目录（覆盖非默认盘符与带版本号后缀的 JLink_Vxxx 目录）
    #    跨盘符收集所有候选，按修改时间降序，自动优先最新版本
    base_dirs = [
        r"C:\Program Files\SEGGER",
        r"C:\Program Files (x86)\SEGGER",
        r"D:\Program Files\SEGGER",
        r"D:\Program Files (x86)\SEGGER",
        r"E:\Program Files\SEGGER",
        r"E:\Program Files (x86)\SEGGER",
    ]
    candidates = []
    for base in base_dirs:
        if not os.path.isdir(base):
            continue
        candidates.append(os.path.join(base, "JLink_x64.dll"))
        try:
            for entry in os.listdir(base):
                sub = os.path.join(base, entry)
                if entry.lower().startswith("jlink") and os.path.isdir(sub):
                    candidates.append(os.path.join(sub, "JLink_x64.dll"))
        except Exception:
            pass
    # 去重、过滤存在的、按修改时间降序（最新版本优先）
    existing = []
    for c in candidates:
        if c not in existing and os.path.isfile(c):
            existing.append(c)
    if existing:
        existing.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return existing[0]

    # 3. 系统 PATH
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not path_dir:
            continue
        candidate = os.path.join(path_dir, "JLink_x64.dll")
        if os.path.isfile(candidate):
            return candidate

    return None


# 全局日志记录器
logger = setup_logging()
