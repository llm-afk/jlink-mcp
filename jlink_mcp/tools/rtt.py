"""绑定当前连接会话的 RTT (Real Time Transfer) 工具。"""

import codecs
import time
from typing import Dict, Any, Optional

from ..jlink_manager import jlink_manager
from ..exceptions import JLinkMCPError, JLinkErrorCode, RTTError
from ..utils import logger


_rtt_started = False
_rtt_session = None
_rtt_link = None
_rtt_config = {"buffer_index": 0, "read_mode": "continuous", "timeout_ms": 1000}
_decoders = {}


def _clear_state() -> None:
    global _rtt_started, _rtt_session, _rtt_link
    _rtt_started = False
    _rtt_session = None
    _rtt_link = None
    _decoders.clear()


def _cleanup_session(link) -> None:
    """关闭探针前停止当前 RTT；即使驱动报错也释放会话状态。"""
    try:
        if _rtt_started and _rtt_link is link:
            link.rtt_stop()
    finally:
        _clear_state()


jlink_manager.add_cleanup_callback(_cleanup_session)


def _sync_state() -> None:
    if _rtt_started and (jlink_manager.session_id != _rtt_session or not jlink_manager.is_connected):
        _clear_state()


def _active_link():
    _sync_state()
    if not _rtt_started:
        raise RTTError(JLinkErrorCode.RTT_NOT_STARTED, "当前连接尚未启动 RTT，请调用 rtt_start")
    return jlink_manager.get_jlink()


def _integer(value, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RTTError(JLinkErrorCode.INVALID_PARAMETER,
                       f"{name} 必须是 {minimum} 到 {maximum} 之间的整数")
    return value


def _buffer(value: Optional[int]) -> int:
    return _integer(_rtt_config["buffer_index"] if value is None else value, "buffer_index", 0, 255)


def _failure(exc: Exception, **fields) -> Dict[str, Any]:
    logger.error(f"RTT 操作失败: {exc}")
    error = exc.to_dict() if isinstance(exc, JLinkMCPError) else {
        "code": JLinkErrorCode.UNKNOWN_ERROR.code,
        "description": str(exc), "suggestion": "请检查 RTT 和设备连接状态",
    }
    return {"success": False, **fields, "error": error}


def rtt_start(
    buffer_index: int = 0,
    read_mode: str = "continuous",
    timeout_ms: int = 1000,
    block_address: Optional[int] = None,
) -> Dict[str, Any]:
    """启动当前会话 RTT。

    continuous 读取时最多等待 timeout_ms；once 只读取一次。
    block_address 可指定 RTT 控制块地址；后续省略缓冲区索引时使用本次配置。
    """
    global _rtt_started, _rtt_session, _rtt_link, _rtt_config
    try:
        _integer(buffer_index, "buffer_index", 0, 255)
        _integer(timeout_ms, "timeout_ms", 0, 60000)
        if read_mode not in ("continuous", "once"):
            raise RTTError(JLinkErrorCode.INVALID_PARAMETER, "read_mode 仅支持 continuous/once")
        if block_address is not None:
            _integer(block_address, "block_address", 0, 0xFFFFFFFF)
        _sync_state()
        if _rtt_started:
            raise RTTError(JLinkErrorCode.RTT_ALREADY_STARTED, "RTT 已在当前连接运行")
        link = jlink_manager.get_jlink()
        link.rtt_start(block_address)
        _rtt_link = link
        _rtt_session = jlink_manager.session_id
        _rtt_config = {"buffer_index": buffer_index, "read_mode": read_mode, "timeout_ms": timeout_ms}
        _decoders.clear()
        _rtt_started = True
        return {"success": True, "buffer_index": buffer_index, "message": f"RTT 已启动（缓冲区 {buffer_index}）"}
    except Exception as exc:
        return _failure(exc, buffer_index=buffer_index)


def rtt_stop() -> Dict[str, Any]:
    """停止当前 RTT，并清理分块文本解码状态。"""
    try:
        link = _active_link()
        try:
            link.rtt_stop()
        finally:
            _clear_state()
        return {"success": True, "message": "RTT 已停止"}
    except Exception as exc:
        return _failure(exc)


def rtt_read(
    buffer_index: Optional[int] = None,
    size: int = 1024,
    timeout_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """读取至多 size 字节；按会话及缓冲区保留 UTF-8 分块解码状态。

    continuous 在缓冲区为空时最多轮询 timeout_ms；once 为单次非阻塞读取。
    data_hex 包含本次原始字节，data 可能等待后续字节组成完整 UTF-8 字符。
    """
    try:
        link = _active_link()
        index = _buffer(buffer_index)
        _integer(size, "size", 1, 1024 * 1024)
        timeout = _integer(_rtt_config["timeout_ms"] if timeout_ms is None else timeout_ms,
                           "timeout_ms", 0, 60000)
        deadline = time.monotonic() + timeout / 1000.0
        while True:
            data = bytes(link.rtt_read(index, size))
            if data or _rtt_config["read_mode"] == "once" or timeout == 0:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.01, remaining))
            link = _active_link()
        decoder = _decoders.setdefault(index, codecs.getincrementaldecoder("utf-8")(errors="replace"))
        text = decoder.decode(data, final=False)
        return {
            "success": True, "data": text, "data_hex": data.hex(), "bytes_read": len(data),
            "message": f"成功读取 {len(data)} 字节" if data else "RTT 缓冲区为空",
        }
    except Exception as exc:
        return _failure(exc, data="", data_hex="", bytes_read=0)


def rtt_write(data: str, buffer_index: Optional[int] = None) -> Dict[str, Any]:
    """向 RTT 下行缓冲区写入 UTF-8 文本，默认使用启动时配置的索引。"""
    try:
        link = _active_link()
        index = _buffer(buffer_index)
        if not isinstance(data, str):
            raise RTTError(JLinkErrorCode.INVALID_PARAMETER, "data 必须为字符串")
        count = link.rtt_write(index, data.encode("utf-8"))
        return {"success": True, "bytes_written": count, "message": f"成功写入 {count} 字节"}
    except Exception as exc:
        return _failure(exc, bytes_written=0)


def rtt_get_status() -> Dict[str, Any]:
    """返回当前连接的 RTT 状态，旧连接状态不会沿用到新连接。"""
    _sync_state()
    return {
        "success": True, "started": _rtt_started,
        "buffer_index": _rtt_config["buffer_index"] if _rtt_started else None,
        "config": _rtt_config.copy() if _rtt_started else None,
        "message": "RTT 已启动" if _rtt_started else "RTT 未启动",
    }
