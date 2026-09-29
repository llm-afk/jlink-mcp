"""Bounded RTT sessions, incremental UTF-8 decoding and complete writes."""
import codecs
import time
from ..jlink_manager import jlink_manager
from ..exceptions import JLinkMCPError, JLinkErrorCode
from ..utils import logger

_rtt_started = False
_rtt_owner = None
_rtt_config = {}
_decoders = {}


def reset_rtt_state(jlink=None):
    global _rtt_started, _rtt_owner, _rtt_config
    if _rtt_started and jlink is not None and jlink is _rtt_owner:
        try:
            jlink.rtt_stop()
        except Exception as exc:
            logger.debug(f'RTT cleanup: {exc}')
    _rtt_started, _rtt_owner, _rtt_config = False, None, {}
    _decoders.clear()


def _failure(exc, **fields):
    if not isinstance(exc, JLinkMCPError):
        exc = JLinkMCPError(JLinkErrorCode.READ_FAILED, str(exc))
    return {'success': False, **fields, 'error': exc.to_dict()}


def _bounded(value, maximum, name):
    if not isinstance(value, int) or not 0 <= value <= maximum:
        raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, f'{name} 必须在 0–{maximum}')
    return value


def _session():
    jlink = jlink_manager.get_jlink()
    if not _rtt_started or jlink is not _rtt_owner:
        reset_rtt_state()
        raise JLinkMCPError(JLinkErrorCode.RTT_NOT_STARTED)
    return jlink


def rtt_start(buffer_index=0, read_mode='continuous', timeout_ms=1000, block_address=None):
    """Arm RTT search; repeat configuration is idempotent. once polls immediately."""
    global _rtt_started, _rtt_owner, _rtt_config
    try:
        _bounded(buffer_index, 255, 'buffer_index')
        _bounded(timeout_ms, 30000, 'timeout_ms')
        if read_mode not in ('continuous', 'once'):
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, 'read_mode 必须为 continuous/once')
        if block_address is not None and (not 0 <= block_address <= 0xFFFFFFFF or block_address % 4):
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, 'block_address 必须是 32 位、4 字节对齐地址')
        jlink = jlink_manager.get_jlink()
        config = dict(buffer_index=buffer_index, read_mode=read_mode, timeout_ms=timeout_ms, block_address=block_address)
        if _rtt_started and _rtt_owner is jlink and config == _rtt_config:
            return {'success': True, 'buffer_index': buffer_index, 'already_started': True, 'message': 'RTT 已启动'}
        reset_rtt_state(jlink)
        jlink.rtt_start(block_address)
        _rtt_started, _rtt_owner, _rtt_config = True, jlink, config
        return {'success': True, 'buffer_index': buffer_index, 'message': 'RTT 搜索已启动，请读取日志确认目标通道'}
    except Exception as exc:
        return _failure(exc, buffer_index=buffer_index)


def rtt_stop():
    try:
        if _rtt_started:
            _session().rtt_stop()
        return {'success': True, 'message': 'RTT 已停止'}
    except Exception as exc:
        return _failure(exc)
    finally:
        reset_rtt_state()


def _options(buffer_index, timeout_ms):
    index = _bounded(_rtt_config.get('buffer_index', 0) if buffer_index is None else buffer_index, 255, 'buffer_index')
    timeout = _bounded(_rtt_config.get('timeout_ms', 1000) if timeout_ms is None else timeout_ms, 30000, 'timeout_ms')
    return index, timeout


def rtt_read(buffer_index=None, size=1024, timeout_ms=None):
    """Wait for first data; keep split UTF-8 state per channel and expose raw hex."""
    try:
        index, timeout = _options(buffer_index, timeout_ms)
        if not 1 <= size <= 65536:
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, 'size 必须在 1–65536 字节')
        jlink = _session()
        if _rtt_config.get('read_mode') == 'once':
            timeout = 0
        deadline = time.monotonic() + timeout / 1000
        while True:
            data = bytes(jlink.rtt_read(index, size))
            if data or time.monotonic() >= deadline:
                break
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        decoder = _decoders.setdefault(index, codecs.getincrementaldecoder('utf-8')(errors='replace'))
        return {'success': True, 'data': decoder.decode(data, final=False), 'data_hex': data.hex(),
                'bytes_read': len(data), 'pending_utf8_bytes': len(decoder.getstate()[0]),
                'timed_out': not data and timeout > 0,
                'message': f'读取 {len(data)} 字节' if data else '未读到数据；检查目标运行状态及 RTT 控制块地址'}
    except Exception as exc:
        return _failure(exc, data='', bytes_read=0)


def rtt_write(data, buffer_index=None, timeout_ms=None):
    """Retry only the unwritten suffix until complete or timed out."""
    written = 0
    try:
        index, timeout = _options(buffer_index, timeout_ms)
        payload = data.encode('utf-8')
        if not 1 <= len(payload) <= 65536:
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, 'data 必须为 1–65536 字节 UTF-8 数据')
        jlink = _session()
        deadline = time.monotonic() + timeout / 1000
        while written < len(payload):
            count = jlink.rtt_write(index, payload[written:])
            if not 0 <= count <= len(payload) - written:
                raise JLinkMCPError(JLinkErrorCode.WRITE_FAILED, 'RTT 返回无效写入长度')
            written += count
            if written == len(payload):
                return {'success': True, 'bytes_written': written, 'complete': True, 'message': f'已发送 {written} 字节'}
            if time.monotonic() >= deadline:
                raise JLinkMCPError(JLinkErrorCode.OPERATION_TIMEOUT, f'仅发送 {written}/{len(payload)} 字节；不要重发已写入部分，检查目标运行状态/下行缓冲区')
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    except Exception as exc:
        return _failure(exc, bytes_written=written, complete=False)


def rtt_get_status():
    if _rtt_started:
        try:
            _session()
        except Exception:
            reset_rtt_state()
    return {'success': True, 'started': _rtt_started,
            'buffer_index': _rtt_config.get('buffer_index') if _rtt_started else None,
            'config': dict(_rtt_config) if _rtt_started else None,
            'message': 'RTT 已启动（不代表已发现目标控制块）' if _rtt_started else 'RTT 未启动'}
