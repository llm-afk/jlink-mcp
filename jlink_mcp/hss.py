"""Bounded DLL-side high-speed sampling of matched RAM variables."""
import csv
import ctypes as ct
import math
import struct
import time
import uuid
from pathlib import Path

from pylink.enums import JLinkCPUCapabilities


class Unsupported(RuntimeError):
    code = "HSS_UNSUPPORTED"


class StopError(RuntimeError):
    code = "HSS_STOP_FAILED"


class Block(ct.Structure):
    _fields_ = [("address", ct.c_uint32), ("size", ct.c_uint32),
                ("flags", ct.c_uint32), ("reserved", ct.c_uint32)]


def layout(fields, regions):
    """Pack the union of requested bytes without gaps or crossing RAM regions."""
    spans = []
    for f in sorted(fields, key=lambda f: f["address"]):
        start, end = f["address"], f["address"] + f["size"]
        region = next((r for r in regions if r.kind == "ram" and r.address <= start and end <= r.address + r.size), None)
        if region is None:
            raise ValueError("HSS field lies outside profiled RAM")
        if spans and start <= spans[-1][1] and region == spans[-1][2]:
            spans[-1] = (spans[-1][0], max(end, spans[-1][1]), region)
        else:
            spans.append((start, end, region))
    columns = []
    for field in fields:
        kind, size = field["type"]["kind"], field["size"]
        if kind == "float":
            fmt = {4: "f", 8: "d"}.get(size)
        elif kind in ("signed", "unsigned", "enum", "bool", "address", "pointer"):
            fmt = {1: "B", 2: "H", 4: "I", 8: "Q"}.get(size)
            if fmt and (kind == "signed" or kind == "enum" and field["type"]["signed"]):
                fmt = fmt.lower()
        else:
            fmt = None
        if not fmt:
            raise ValueError("Select a scalar variable, struct member or array element for HSS")
        offset = 4  # JLINK_HSS_FLAG_TIMESTAMP_US, followed by packed block bytes.
        for start, end, _ in spans:
            if start <= field["address"] < end:
                offset += field["address"] - start
                break
            offset += end - start
        columns.append((offset, struct.Struct("<" + fmt)))
    return [(start, end - start) for start, end, _ in spans], columns


class NativeHss:
    def __init__(self, probe):
        if not probe.cpu_capability(JLinkCPUCapabilities.HSS):
            raise Unsupported("Probe/CPU combination does not report HSS support")
        try:
            dll = probe._dll  # PyLink has no public HSS transfer methods.
            self.start = dll.JLINK_HSS_Start
            self.read = dll.JLINK_HSS_Read
            self.stop = dll.JLINK_HSS_Stop
            caps = dll.JLINK_HSS_GetCaps
        except AttributeError as exc:
            raise Unsupported("Installed J-Link DLL lacks the HSS API") from exc
        self.start.argtypes = [ct.POINTER(Block), ct.c_int, ct.c_int, ct.c_int]
        self.read.argtypes = [ct.c_void_p, ct.c_uint32]
        self.stop.argtypes = []
        caps.argtypes = [ct.POINTER(ct.c_uint32)]
        values = (ct.c_uint32 * 8)()
        if caps(values) < 0:
            raise Unsupported("J-Link could not query HSS capabilities")
        self.max_blocks, self.max_hz = values[:2]


def capture(probe, request, fields, regions):
    blocks, columns = layout(fields, regions)
    period = request.period_us
    frame = 4 + sum(size for _, size in blocks)
    total = request.sample_count * frame
    if total > 8 * 1024 * 1024:
        raise ValueError("HSS raw capture exceeds 8 MiB")
    driver = NativeHss(probe)
    if len(blocks) > driver.max_blocks or period * driver.max_hz < 1000000:
        raise Unsupported(f"Probe limits: {driver.max_blocks} blocks, {driver.max_hz} Hz")
    descriptors = (Block * len(blocks))(*(Block(a, n, 0, 0) for a, n in blocks))
    window_us = None
    if request.count is None:
        window_us = request.duration_ms * 1000 if request.duration_ms else max(1000000, 2 * period)
    limit = request.count or min(100000, (8 * 1024 * 1024) // frame)
    # One look-ahead timestamp distinguishes a finished window from a frame limit.
    capacity = (limit + (window_us is not None)) * frame
    buffer = ct.create_string_buffer(max(1, min(65536, capacity) // frame) * frame)
    raw = bytearray()
    path = Path(request.output_path or f"captures/{uuid.uuid4().hex}.csv").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fault = None
    finished = False
    time_column = "timestamp_us" if "timestamp_us" not in request.items else "timestamp_us (HSS)"
    with path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow([time_column, *request.items])
        if probe.halted():
            raise ValueError("Target halted before HSS start")
        rc = driver.start(descriptors, len(blocks), period, 1)
        if rc < 0:
            raise Unsupported(f"HSS start failed ({rc}); requested {len(blocks)} blocks at {period} us")
        try:
            budget_us = window_us or min(10000000, 2 * request.sample_count * period)
            deadline = time.perf_counter() + budget_us / 1e6 + 1.0
            while not finished and time.perf_counter() < deadline:
                n = driver.read(buffer, len(buffer))
                if not 0 <= n <= len(buffer):
                    fault = f"HSS read returned {n}"
                    break
                if not n:
                    time.sleep(0.001)
                    continue
                previous = len(raw) // frame
                raw.extend(memoryview(buffer).cast("B")[:min(n, capacity - len(raw))])
                frames = len(raw) // frame
                if window_us is None:
                    finished = frames == limit
                elif frames > previous:
                    origin = struct.unpack_from("<I", raw)[0]
                    latest = struct.unpack_from("<I", raw, (frames - 1) * frame)[0]
                    span = (latest - origin) & 0xffffffff
                    if span > 12000000:
                        fault = "HSS timestamp discontinuity"
                        break
                    if span >= window_us:
                        # Only scan the final native batch to locate the window edge.
                        for i in range(previous, frames):
                            tick = struct.unpack_from("<I", raw, i * frame)[0]
                            if ((tick - origin) & 0xffffffff) >= window_us:
                                del raw[i * frame:]
                                finished = True
                                break
                    elif frames > limit:
                        del raw[limit * frame:]
                        fault = "HSS capture reached its frame or memory limit"
                        break
        except Exception as exc:
            fault = f"HSS read failed: {exc}"
        finally:
            try:
                rc = driver.stop()
            except Exception as exc:
                raise StopError("HSS stop was not confirmed; closing session") from exc
            if rc < 0:
                raise StopError("Native HSS stop failed; closing session to release the probe")

        count = len(raw) // frame
        first = last = None
        elapsed = largest = 0
        gaps = 0
        last_row = []
        for pos in range(0, count * frame, frame):
            tick = struct.unpack_from("<I", raw, pos)[0]
            if first is None:
                first = tick
            else:
                delta = (tick - last) & 0xffffffff
                if delta > 12000000:
                    fault = "HSS timestamp discontinuity"
                largest = max(largest, delta)
                gaps += delta > 2 * period
                elapsed += delta
            last = tick
            last_row = [value.unpack_from(raw, pos + off)[0] for off, value in columns]
            writer.writerow([first + elapsed, *last_row])

    complete = finished and fault is None
    result = dict(success=complete, complete=complete, sample_count=count, path=str(path),
                  period_us=period, actual_hz=round((count - 1) * 1e6 / elapsed, 2) if elapsed and not fault else None,
                  max_interval_us=largest, intervals_over_2x_period=gaps, blocks=len(blocks),
                  clock="J-Link HSS timestamp in microseconds", time_column=time_column, frame_bytes=frame,
                  captured_span_us=elapsed, requested_rate_hz=request.rate_hz,
                  last_values={name: value if math.isfinite(value) else str(value)
                               for name, value in zip(request.items, last_row)})
    if not complete:
        result["error"] = {"code": "HSS_INCOMPLETE", "message": fault or "Capture deadline reached"}
    return result
