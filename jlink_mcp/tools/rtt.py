"""Bounded text and binary RTT transfers."""
import base64
import codecs
import hashlib
import time
from pathlib import Path
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


def _failure(exc, code=JLinkErrorCode.READ_FAILED, **fields):
    if not isinstance(exc, JLinkMCPError):
        exc = JLinkMCPError(code, str(exc))
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


def _backoff(delay, remaining):
    time.sleep(min(delay, remaining))
    return min(0.010, delay * 2)


def _read_until(jlink, index, size, deadline):
    delay = 0.001
    while True:
        data = bytes(jlink.rtt_read(index, size))
        if data:
            return data
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return b''
        delay = _backoff(delay, remaining)
        if time.perf_counter() >= deadline:
            return b''


def rtt_read(buffer_index=None, size=1024, timeout_ms=None, data_format='text_hex'):
    """Read the first available chunk. Only consecutive text reads share a decoder."""
    try:
        index, timeout = _options(buffer_index, timeout_ms)
        if not 1 <= size <= 65536:
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, 'size 必须在 1–65536 字节')
        if data_format not in ('text_hex', 'text', 'base64'):
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, 'format 必须为 text_hex/text/base64')
        jlink = _session()
        if _rtt_config.get('read_mode') == 'once':
            timeout = 0
        data = _read_until(jlink, index, size, time.perf_counter() + timeout / 1000)
        result = {'success': True, 'bytes_read': len(data), 'timed_out': not data and timeout > 0,
                  'message': f'读取 {len(data)} 字节' if data else '未读到数据；检查目标运行状态及 RTT 控制块地址'}
        if data_format == 'base64':
            if data:
                _decoders.pop(index, None)
            result['data_b64'] = base64.b64encode(data).decode('ascii')
        else:
            if index not in _decoders:
                _decoders[index] = codecs.getincrementaldecoder('utf-8')(errors='replace')
            decoder = _decoders[index]
            result.update(data=decoder.decode(data), pending_utf8_bytes=len(decoder.getstate()[0]))
            if data_format == 'text_hex':
                result['data_hex'] = data.hex()
        return result
    except Exception as exc:
        return _failure(exc, data='', bytes_read=0)


def rtt_capture(buffer_index, size, timeout_ms, output_path):
    """Drain up to size bytes into a new file under one total deadline."""
    consumed = saved = 0
    path = None
    try:
        index, timeout = _options(buffer_index, timeout_ms)
        if not 1 <= size <= 16 * 1024 * 1024 or timeout < 1:
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, 'capture 的大小必须在 1 字节到 16 MiB 之间，超时必须为正')
        jlink = _session()
        destination = Path(output_path).resolve()
        digest = hashlib.sha256()
        with destination.open('xb', buffering=0) as stream:
            path = str(destination)
            start = time.perf_counter()
            deadline = start + timeout / 1000
            while saved < size and time.perf_counter() < deadline:
                data = _read_until(jlink, index, min(16384, size - saved), deadline)
                if not data:
                    break
                _decoders.pop(index, None)
                consumed += len(data)
                count = stream.write(data)
                saved += count
                if count != len(data):
                    raise JLinkMCPError(JLinkErrorCode.WRITE_FAILED, '采集文件写入不完整')
                digest.update(data)
        return {'success': True, 'complete': saved == size, 'timed_out': saved < size,
                'bytes_read': consumed, 'bytes_written': saved, 'path': path,
                'sha256': digest.hexdigest(), 'elapsed_ms': round((time.perf_counter() - start) * 1000, 3)}
    except Exception as exc:
        return _failure(exc, code=JLinkErrorCode.WRITE_FAILED if isinstance(exc, OSError) else JLinkErrorCode.READ_FAILED,
                        complete=False, bytes_read=consumed, bytes_written=saved, path=path)


def rtt_write(data=None, buffer_index=None, timeout_ms=None, *, data_b64=None):
    """Immediately submit the next suffix on progress, back off on a full buffer."""
    written = 0
    try:
        index, timeout = _options(buffer_index, timeout_ms)
        if (data is None) == (data_b64 is None):
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, '请提供 text 或 data_b64 中的一项')
        if data_b64 is None:
            payload = data.encode('utf-8')
        else:
            try:
                payload = base64.b64decode(data_b64, validate=True)
            except ValueError as exc:
                raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, '无效的 Base64 数据') from exc
        if not 1 <= len(payload) <= 65536:
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, '写入载荷必须为 1–65536 字节')
        jlink = _session()
        deadline = time.perf_counter() + timeout / 1000
        delay = 0.001
        chunk_size = 4096 if timeout else len(payload)
        while written < len(payload):
            chunk = payload[written:written + chunk_size]
            count = jlink.rtt_write(index, chunk)
            if not 0 <= count <= len(chunk):
                raise JLinkMCPError(JLinkErrorCode.WRITE_FAILED, 'RTT 返回无效写入长度')
            written += count
            if written == len(payload):
                return {'success': True, 'bytes_written': written, 'complete': True, 'message': f'已发送 {written} 字节'}
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            if count:
                delay = 0.001
            else:
                delay = _backoff(delay, remaining)
                if time.perf_counter() >= deadline:
                    break
        raise JLinkMCPError(JLinkErrorCode.OPERATION_TIMEOUT, f'仅发送 {written}/{len(payload)} 字节，请检查目标运行状态和下行缓冲区')
    except Exception as exc:
        return _failure(exc, code=JLinkErrorCode.WRITE_FAILED, bytes_written=written, complete=False, retry_safe=False)


def rtt_get_status(include_driver=False):
    if _rtt_started:
        try:
            jlink = _session()
        except Exception:
            reset_rtt_state()
    result = {'success': True, 'started': _rtt_started,
              'buffer_index': _rtt_config.get('buffer_index') if _rtt_started else None,
              'config': dict(_rtt_config) if _rtt_started else None,
              'message': 'RTT 已启动（不代表已发现目标控制块）' if _rtt_started else 'RTT 未启动'}
    if include_driver and _rtt_started:
        try:
            status = jlink.rtt_get_status()
            result['driver'] = {'running': bool(status.IsRunning), 'up_buffers': status.NumUpBuffers,
                                'down_buffers': status.NumDownBuffers, 'host_overflows': status.HostOverflowCount,
                                'bytes_from_target': status.NumBytesRead, 'bytes_to_client': status.NumBytesTransferred}
        except Exception as exc:
            result['driver_error'] = str(exc)
    return result
