"""Serialized debug sessions behind the compact MCP API.

This layer owns session identity and observation semantics. Only control and
firmware operations may deliberately change execution state. Driver calls are
never automatically retried after an uncertain result.
"""
import copy
import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import api_models as contracts
from .gdb_server import gdb_server_manager
from .jlink_manager import jlink_manager
from .symbols import SymbolFile
from .profiles import load_profile
from .image_match import compare_image
from .source_context import disassemble
from .fault_context import recover_frame, decode_fault_flags
from .dwarf_variables import decode
from .svd_manager import svd_manager
from .target_access import read_bytes, write_bytes, validate_span, register_name
from .tools import connection, debug, flash, rtt, svd


class ServiceError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def error_result(exc):
    if hasattr(exc, "to_dict"):
        return {"success": False, "error": exc.to_dict()}
    return {"success": False, "error": {"code": getattr(exc, "code", "OPERATION_FAILED"),
                                       "message": str(exc)}}


class DebugService:
    def __init__(self):
        self.lock = threading.RLock()
        self.session_id = None
        self.target = None
        self.probe = None
        self.symbols = None
        self.generation = 0
        self.snapshots = {}
        self.breakpoints = {}
        self.profile = None
        self.profile_identity = None
        self.image_match = {"status": "unknown", "reason": "not checked"}

    def _state(self):
        if self.probe is None:
            return "disconnected"
        try:
            return "halted" if self.probe.halted() else "running"
        except Exception:
            return "unknown"

    def _require(self, session_id, check_probe=True):
        if not self.session_id or session_id != self.session_id:
            raise ServiceError("STALE_SESSION", "Open a session and use its current session_id")
        if not check_probe:
            return
        if gdb_server_manager.is_running:
            raise ServiceError("BACKEND_BUSY", "GDB Server owns the probe; close it before DLL operations")
        try:
            probe = jlink_manager.get_jlink()
        except Exception as exc:
            self._invalidate()
            raise ServiceError("SESSION_LOST", "Target unavailable; close and reopen the session") from exc
        if probe is not self.probe:
            self._invalidate()
            raise ServiceError("SESSION_LOST", "Probe connection changed; close and reopen the session")

    def _invalidate(self):
        self.generation += 1
        self.snapshots.clear()
        self.image_match = {"status": "unknown", "reason": "session or target state changed"}

    def invoke(self, method, request=None):
        # Shared across *all* tools, including read/capture. No overlapping calls
        # into the DLL or replacement of a connection during a long operation.
        with self.lock:
            started, timestamp = time.monotonic(), utc_now()
            before = "unknown"
            authorized = False
            try:
                if isinstance(request, contracts.Bound):
                    self._require(request.session_id, not isinstance(request, contracts.CloseSession))
                authorized = True
                before = self._state()
                result = getattr(self, method)(request)
            except Exception as exc:
                result = error_result(exc)
                if method in ("write", "control", "firmware"):
                    result["completion"] = "unknown"
                    result["retry_safe"] = False
            result["meta"] = {"session_id": self.session_id, "target": copy.deepcopy(self.target),
                              "generation": self.generation, "timestamp": timestamp,
                              "duration_ms": round((time.monotonic() - started) * 1000, 3),
                              "cpu_before": before, "cpu_after": self._state() if authorized else "unknown"}
            return result

    def capabilities(self):
        return {"backend": "jlink-dll", "read_kinds": ["memory", "register", "peripheral", "symbol", "variable"],
                "elf": "32-bit little-endian ET_EXEC; static DWARF globals/members/fixed arrays",
                "execution_debug": self.target is not None and self.target["architecture"] == "cortex-m",
                "capture": ["snapshot", "diff", "sample"], "channel": "RTT",
                "control": ["halt", "resume", "reset", "step", "wait", "run_until"],
                "breakpoint_kinds": ["execute", "read", "write", "access"],
                "context": "Cortex-M halted registers, stop reasons, ELF source mapping and Thumb disassembly",
                "unsupported": ["GDB call stack", "source stepping", "arbitrary DWARF expressions and local variables",
                                "RISC-V fault decoding"]}

    def discover(self, _=None):
        return {"success": True, "probes": connection.list_jlink_devices(),
                "capabilities": self.capabilities(), "svd_devices": svd.list_svd_devices()}

    def session(self, request):
        if request.action == "status":
            return {"success": True, "connection": connection.get_connection_status(),
                    "elf": self._elf_identity(), "image_match": copy.deepcopy(self.image_match),
                    "profile": {"identity": self.profile_identity, "config": self.profile.model_dump()} if self.profile else None,
                    "capabilities": self.capabilities(), "channel": rtt.rtt_get_status()}
        if request.action == "close":
            cleanup_errors = []
            try:
                self._remove_owned_breakpoints()
            except Exception as exc:
                cleanup_errors.append(str(exc))
            result = connection.disconnect_device()
            if result.get("success"):
                self._invalidate()
                self.breakpoints.clear()
                self.session_id = self.target = self.probe = self.symbols = None
                self.profile = self.profile_identity = None
            result["cleanup_errors"] = cleanup_errors
            result["success"] = result.get("success", False) and not cleanup_errors
            return result
        if self.session_id or jlink_manager.is_connected or gdb_server_manager.is_running:
            raise ServiceError("BACKEND_BUSY", "Close the existing session/backend before opening another")
        profile, profile_identity = None, None
        if request.profile_path:
            profile, profile_identity = load_profile(request.profile_path)
            for field in ("chip", "architecture"):
                value = getattr(request, field)
                if value is not None and value != getattr(profile, field):
                    raise ServiceError("PROFILE_MISMATCH", f"Cannot override profile {field}; use a matching profile")
        fields = ("chip", "serial_number", "interface", "architecture", "svd_device", "elf_path", "jlink_path")
        resolved = {field: getattr(request, field) if getattr(request, field) is not None
                    else getattr(profile, field, None) for field in fields}
        resolved["interface"] = resolved["interface"] or "SWD"
        resolved["architecture"] = resolved["architecture"] or "unknown"
        request = contracts.OpenSession(action="open", **resolved)
        symbols = SymbolFile(request.elf_path) if request.elf_path else None
        if symbols and request.architecture == "cortex-m" and symbols.machine != "EM_ARM":
            raise ServiceError("ARCH_MISMATCH", "Cortex-M requires an ARM ELF")
        if request.svd_device and request.svd_device not in svd_manager.device_names:
            raise ServiceError("SVD_NOT_FOUND", "Use an exact SVD device name from discover")
        result = connection.connect_device(request.serial_number, request.interface, request.chip, request.jlink_path)
        if not result.get("success"):
            return result
        actual = connection.get_connection_status()
        if not actual.get("success") or not actual.get("data", {}).get("target_connected"):
            connection.disconnect_device()
            raise ServiceError("TARGET_NOT_CONNECTED", "Probe opened but target connection was not confirmed")
        self.probe = jlink_manager.get_jlink()
        self.session_id = uuid.uuid4().hex
        self.target = {"chip": request.chip, "serial_number": request.serial_number,
                       "actual_serial_number": str(actual["data"]["device_serial"]),
                       "interface": request.interface, "architecture": request.architecture,
                       "svd_device": request.svd_device}
        self.symbols = symbols
        self.profile, self.profile_identity = profile, profile_identity
        self._invalidate()
        return {"success": True, "session_id": self.session_id, "capabilities": self.capabilities(),
                "elf": self._elf_identity(), "profile": profile_identity,
                "connection": connection.get_connection_status()}

    def _elf_identity(self):
        return {**self.symbols.identity(), "target_match": self.image_match["status"]} if self.symbols else None

    def _halted(self):
        if not self.probe.halted():
            raise ServiceError("REQUIRES_HALT", "Use control(action='halt') explicitly first")

    def _register_name(self, name):
        return register_name(name) if self.target["architecture"] == "cortex-m" else name

    def _peripheral(self, name):
        device = self.target.get("svd_device")
        if not device or "." not in name:
            raise ServiceError("SVD_REQUIRED", "Set session svd_device and use peripheral.register")
        peripheral_name, register = name.split(".", 1)
        peripheral = svd_manager.get_peripheral(device, peripheral_name)
        info = svd_manager.get_register(device, peripheral_name, register)
        if peripheral is None or info is None:
            raise ServiceError("NOT_FOUND", f"Unknown SVD register {name}")
        validate_span(peripheral.base_address + info.address_offset, info.size // 8, info.size)
        return peripheral.base_address + info.address_offset, info, peripheral_name, register

    def _symbol(self, name):
        if not self.symbols:
            raise ServiceError("ELF_REQUIRED", "Open a session with elf_path")
        return self.symbols.resolve(name)

    def _variable(self, expression):
        if not self.symbols:
            raise ServiceError("ELF_REQUIRED", "Open a session with an ELF containing DWARF")
        info = self.symbols.variable(expression)
        validate_span(info["address"], info["size"])
        return info

    def _plan_read(self, item):
        if item.kind == "register":
            return {"kind": "register", "name": self._register_name(item.name), "bytes": 4}
        if item.kind == "memory":
            address, size, width = item.address, item.size, item.width
        elif item.kind == "symbol":
            symbol = self._symbol(item.name)
            if symbol["kind"] != "STT_OBJECT" or not 0 < symbol["size"] <= 65536:
                raise ServiceError("UNSUPPORTED_SYMBOL", "Reading requires a sized object symbol <=64KB")
            address, size, width = symbol["address"], symbol["size"], 8
        elif item.kind == "variable":
            if self.profile is None:
                raise ServiceError("PROFILE_REQUIRED", "Typed reads require profiled RAM/Flash regions")
            if item.require_match and self.image_match["status"] != "matched":
                raise ServiceError("IMAGE_NOT_MATCHED", "Run firmware verify_image first, or explicitly set require_match=false")
            info = self._variable(item.name)
            if not any(r.address <= info["address"] and info["address"] + info["size"] <= r.address + r.size
                       for r in self.profile.regions):
                raise ServiceError("OUTSIDE_PROFILE", "Variable lies outside configured RAM/Flash")
            return {"kind": "variable", "address": info["address"], "bytes": info["size"], "width": 8, "type": info["type"]}
        else:
            address, info, peripheral, register = self._peripheral(item.name)
            if info.access == "write-only":
                raise ServiceError("WRITE_ONLY", "Cannot read a write-only SVD register")
            if (info.read_action or any(f.read_action for f in info.fields)) and not item.allow_side_effects:
                raise ServiceError("READ_SIDE_EFFECT", "SVD declares read side effects; opt in explicitly")
            size, width = info.size // 8, info.size
        validate_span(address, size, width)
        return {"kind": item.kind, "address": address, "bytes": size, "width": width}

    def _read_one(self, item, plan):
        if item.kind == "register":
            self._halted()
            value = self.probe.register_read(plan["name"]) & 0xFFFFFFFF
            self._halted()
            return {"value": value}
        data = read_bytes(self.probe, plan["address"], plan["bytes"], plan["width"])
        result = {"address": plan["address"], "size": len(data), "data_hex": data.hex()}
        if item.kind == "variable":
            result.update(value=decode(plan["type"], data), type=plan["type"], elf_match=self.image_match["status"])
        if item.kind == "peripheral":
            _, _, peripheral, register = self._peripheral(item.name)
            result["decoded"] = svd_manager.parse_register_value(
                self.target["svd_device"], peripheral, register, int.from_bytes(data, "little"))
        return result

    def read(self, request):
        if request.consistency == "halted":
            self._halted()
        plans = []
        for item in request.items:
            try:
                plans.append(self._plan_read(item))
            except Exception as exc:
                plans.append(error_result(exc))
        if sum(p.get("bytes", 0) for p in plans) > 65536:
            raise ServiceError("LIMIT", "Total batch size must not exceed 64KB")
        results = []
        for item, plan in zip(request.items, plans):
            entry = {"item": item.model_dump(), "timestamp": utc_now()}
            try:
                if "error" in plan:
                    entry.update(plan)
                elif request.consistency == "live" and item.kind == "register":
                    raise ServiceError("REQUIRES_HALT", "CPU registers require consistency='halted'")
                else:
                    if request.consistency == "halted":
                        self._halted()
                    entry.update(success=True, data=self._read_one(item, plan))
            except Exception as exc:
                entry.update(error_result(exc))
            results.append(entry)
        stable = request.consistency != "halted" or self._state() == "halted"
        if not stable:
            for entry in results:
                entry.pop("data", None)
                entry.update(error_result(ServiceError("STATE_CHANGED", "Target ran during halted observation")))
        return {"success": all(e["success"] for e in results), "items": results,
                "consistency": request.consistency, "atomic": False,
                "observation": "halted CPU; DMA/peripherals may still change" if request.consistency == "halted"
                else "sequential live reads; not a simultaneous snapshot"}

    def write(self, request):
        self.image_match = {"status": "unknown", "reason": "write requested; reverify before trusting symbol interpretation"}
        item = request.item
        if request.consistency == "halted" or item.kind == "register":
            self._halted()
        if item.kind == "register":
            if request.consistency != "halted":
                raise ServiceError("REQUIRES_HALT", "CPU register writes require halted consistency")
            name = self._register_name(item.name)
            self.probe.register_write(name, item.value)
            observed = self.probe.register_read(name) & 0xFFFFFFFF if request.verify else None
            matched = observed == item.value if request.verify else None
        else:
            if item.kind == "memory":
                data = bytes.fromhex(item.data_hex)
                address, width = item.address, item.width
            else:
                address, info, _, _ = self._peripheral(item.name)
                if info.access == "read-only":
                    raise ServiceError("READ_ONLY", "Cannot write a read-only SVD register")
                if request.verify and info.access == "write-only":
                    raise ServiceError("WRITE_ONLY", "Cannot verify a write-only SVD register")
                data, width = item.value.to_bytes(info.size // 8, "little"), info.size
            validate_span(address, len(data), width)
            write_bytes(self.probe, address, data, width)
            observed = read_bytes(self.probe, address, len(data), width).hex() if request.verify else None
            matched = observed == data.hex() if request.verify else None
        stable = request.consistency != "halted" or self._state() == "halted"
        return {"success": matched is not False and stable, "submitted": True,
                "verification": "matched" if matched else "mismatch" if matched is False else "not_requested",
                "observed": observed, "state_preserved": stable}

    def _cortex_m(self):
        if self.target["architecture"] != "cortex-m":
            raise ServiceError("UNSUPPORTED_ARCH", "This action requires an explicit cortex-m session profile")

    def control(self, request):
        if request.action == "wait":
            stopped = self._wait_halt(request.timeout_ms)
            return {"success": stopped, "completion": "observed_halted" if stopped else "timeout",
                    "stop": self._stop_context() if stopped else None}
        if request.action == "run_until":
            return self._run_until(request)
        if request.action == "reset":
            if request.mode == "core":
                self._cortex_m()
            self._remove_owned_breakpoints()
            self._invalidate()
            result = debug.reset_target(request.mode)
            result["completion"] = "driver_acknowledged" if result["success"] else "unknown"
            if result["success"] and request.mode in ("halt", "core"):
                self._halted()
                result["completion"] = "observed_halted"
            return result
        if request.action == "halt":
            self.probe.halt()
            expected = True
        elif request.action == "resume":
            self.probe.restart()
            if self.probe.halted():
                stop = self._stop_context()
                if any(r["name"] in ("code_breakpoint", "data_watchpoint", "vector_catch") for r in stop["reasons"]):
                    return {"success": True, "completion": "observed_halted", "running_observed": False,
                            "stop": stop, "note": "Resume submitted; target was already halted when sampled"}
            expected = False
        else:
            self._cortex_m()
            self._halted()
            self.probe.step(thumb=True)
            expected = True
        deadline = time.monotonic() + request.timeout_ms / 1000
        while self.probe.halted() != expected:
            if time.monotonic() >= deadline:
                return {"success": False, "completion": "timeout", "submitted": True,
                        "error": {"code": "TIMEOUT", "message": "Requested state was not observed; no retry/reset performed"}}
            time.sleep(0.005)
        return {"success": True, "completion": "observed_halted" if expected else "observed_running",
                "stop": self._stop_context() if expected else None}

    def _wait_halt(self, timeout_ms):
        deadline = time.monotonic() + timeout_ms / 1000
        while not self.probe.halted():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def _stop_context(self):
        self._halted()
        result = {"reasons": [], "registers": {}, "errors": []}
        names = {0: "debug_request", 1: "code_breakpoint", 2: "data_watchpoint", 3: "vector_catch"}
        try:
            for reason in self.probe.cpu_halt_reasons():
                result["reasons"].append({"code": int(reason.HaltReason), "name": names.get(reason.HaltReason, "unknown"),
                                          "unit_index": int(reason.Index)})
        except Exception as exc:
            result["errors"].append({"item": "halt_reasons", "error": str(exc)})
        for name in ("PC", "LR", "SP", "XPSR"):
            try:
                result["registers"][name] = self.probe.register_read(self._register_name(name)) & 0xffffffff
            except Exception as exc:
                result["errors"].append({"item": name, "error": str(exc)})
        self._halted()
        return result

    def _execution_address(self, address, symbol):
        if symbol is not None:
            info = self._symbol(symbol)
            if info["kind"] != "STT_FUNC":
                raise ServiceError("UNSUPPORTED_SYMBOL", "Execution breakpoint requires a function symbol")
            if self.image_match["status"] != "matched":
                raise ServiceError("IMAGE_NOT_MATCHED", "Symbol breakpoints require firmware verify_image; use an explicit address for investigation")
            address = info["address"]
        return address & ~1

    def _run_until(self, request):
        self._cortex_m()
        self._halted()
        address = self._execution_address(request.address, request.symbol)
        if self.probe.register_read(self._register_name("PC")) & ~1 == address:
            return {"success": True, "completion": "already_at_target", "address": address, "stop": self._stop_context()}
        previous = set(self.breakpoints)
        bp = self.breakpoint(contracts.BreakpointAdd(action="set", session_id=request.session_id, address=address))
        key = bp["breakpoint_id"]
        result = {"success": False, "address": address, "on_timeout": request.on_timeout}
        try:
            self.probe.restart()
            stopped = self._wait_halt(request.timeout_ms)
            if not stopped and request.on_timeout == "halt":
                self.probe.halt()
                if not self._wait_halt(1000):
                    raise ServiceError("HALT_FAILED", "Run-until timed out and requested halt was not observed")
            if stopped or request.on_timeout == "halt":
                result["stop"] = self._stop_context()
            stop = result.get("stop", {})
            hit = stopped and (stop.get("registers", {}).get("PC", -1) & ~1) == address
            # Arrival is only attributed to a breakpoint when the driver says so.
            hit = hit and any(r["name"] == "code_breakpoint" for r in stop.get("reasons", []))
            result.update(success=hit, completion="target_reached" if hit else "stopped_elsewhere" if stopped else "timeout")
        except Exception as exc:
            result.update(error_result(exc), completion="unknown", retry_safe=False)
        finally:
            if key not in previous:
                try:
                    self._clear_breakpoint(key)
                    result["temporary_breakpoint_removed"] = True
                except Exception as exc:
                    result.update(success=False, temporary_breakpoint_removed=False,
                                  cleanup_error=str(exc), remaining_breakpoint_id=key)
            else:
                result["reused_breakpoint_id"] = key
        return result

    def breakpoint(self, request):
        if request.action == "list":
            return {"success": True, "breakpoints": copy.deepcopy(self.breakpoints), "scope": "owned by this session"}
        self._cortex_m()
        self._halted()
        if request.action == "remove":
            self._clear_breakpoint(request.breakpoint_id)
            return {"success": True}
        kind, size, address = request.kind, request.size, request.address
        if kind == "execute":
            address = self._execution_address(address, request.symbol)
        else:
            if request.variable:
                plan = self._plan_read(contracts.VariableItem(kind="variable", name=request.variable))
                address = plan["address"]
                if size is not None and size != plan["bytes"]:
                    raise ServiceError("INVALID_SIZE", "Watchpoint size must equal the selected variable size")
                size = plan["bytes"]
            if size not in (1, 2, 4):
                raise ServiceError("INVALID_SIZE", "Select a 1/2/4-byte variable or member")
            validate_span(address, size, size * 8)
        for key, info in self.breakpoints.items():
            if info["address"] == address and info.get("kind", "execute") == kind and info.get("size") == size:
                return {"success": True, "breakpoint_id": key, **info}
        if kind == "execute":
            handle = self.probe.hardware_breakpoint_set(address, thumb=True)
        else:
            # Ignore all data bits: this is an access watchpoint, not a value==0 condition.
            handle = self.probe.watchpoint_set(address, data_mask=0xffffffff, access_size=size * 8,
                                              read=kind in ("read", "access"), write=kind in ("write", "access"))
        if not handle:
            raise ServiceError("BREAKPOINT_FAILED", "Driver did not return a breakpoint handle")
        key = uuid.uuid4().hex
        self.breakpoints[key] = {"address": address, "handle": handle, "kind": kind, "size": size,
                                 "implementation": "hardware"}
        return {"success": True, "breakpoint_id": key, **self.breakpoints[key]}

    def _clear_breakpoint(self, key):
        info = self.breakpoints.get(key)
        if info is None:
            raise ServiceError("NOT_FOUND", "Unknown session breakpoint_id")
        clear = self.probe.breakpoint_clear if info.get("kind", "execute") == "execute" else self.probe.watchpoint_clear
        if not clear(info["handle"]):
            raise ServiceError("BREAKPOINT_FAILED", "Driver did not confirm breakpoint/watchpoint removal")
        del self.breakpoints[key]

    def _remove_owned_breakpoints(self):
        for key in list(self.breakpoints):
            self._clear_breakpoint(key)

    def _in_memory(self, address, size, ram_only=False):
        return self.profile is not None and any(
            (not ram_only or r.kind == "ram") and r.address <= address and address + size <= r.address + r.size
            for r in self.profile.regions)

    def _location(self, address):
        return {**self.symbols.locate(address), "elf": self._elf_identity()} if self.symbols else {
            "address": address, "functions": [], "sources": [], "note": "No ELF loaded"}

    def _inspect_context(self, request):
        self._cortex_m()
        stop = self._stop_context()
        result = {"success": not stop["errors"], "stop": stop, "locations": {}, "instructions": [], "errors": []}
        for name in ("PC", "LR"):
            value = stop["registers"].get(name)
            if value is not None:
                try:
                    result["locations"][name] = self._location(value)
                except Exception as exc:
                    result["errors"].append({"item": name, "error": str(exc)})
        address = request.address if request.address is not None else stop["registers"].get("PC")
        if address is not None and request.instructions:
            address &= ~1
            try:
                size = request.instructions * 4
                if self.profile:
                    regions = [r for r in self.profile.regions if r.address <= address < r.address + r.size]
                    size = min(size, regions[0].address + regions[0].size - address) if regions else 0
                else:
                    size = min(size, self.symbols.executable_span(address)) if self.symbols else 0
                if size < 2:
                    raise ServiceError("OUTSIDE_PROFILE", "Disassembly needs profiled memory or an ELF executable section")
                data = read_bytes(self.probe, address, size)
                result["instructions"] = disassemble(data, address, request.instructions)
                result["instruction_bytes"] = {"address": address, "data_hex": data.hex(), "source": "target memory"}
                result["disassembly_complete"] = len(result["instructions"]) == request.instructions
            except Exception as exc:
                result["errors"].append({"item": "disassembly", "error": str(exc)})
        self._halted()
        result["success"] = result["success"] and not result["errors"]
        return result

    def inspect(self, request):
        if request.action == "context":
            return self._inspect_context(request)
        if request.action == "variable":
            return {"success": True, "variable": self._variable(request.name), "elf": self._elf_identity()}
        if request.action == "preflight":
            if self.profile is None:
                raise ServiceError("PROFILE_REQUIRED", "Preflight requires a project profile")
            checks = []
            if self.profile:
                for check in self.profile.debug_checks:
                    entry = {"name": check.name, "address": check.address, "mask": check.mask,
                             "expected": check.expected, "description": check.description}
                    try:
                        value = int.from_bytes(read_bytes(self.probe, check.address, 4, 32), "little")
                        entry.update(read_success=True, value=value, satisfied=(value & check.mask) == check.expected)
                    except Exception as exc:
                        entry.update(read_success=False, satisfied=None, error=str(exc))
                    checks.append(entry)
            return {"success": all(c["read_success"] for c in checks), "cpu": self._state(),
                    "profile": self.profile_identity, "checks": checks, "image_match": copy.deepcopy(self.image_match),
                    "note": "Observations only; no debug bits, CPU state or Flash changed. Not a general safety verdict."}
        if request.action == "symbol":
            return {"success": True, "symbol": self._symbol(request.name), "elf": self._elf_identity()}
        if request.action == "svd":
            device = self.target.get("svd_device")
            if not device:
                raise ServiceError("SVD_REQUIRED", "Set svd_device when opening the session")
            return svd.get_svd_registers(device, request.peripheral) if request.peripheral else svd.get_svd_peripherals(device)
        self._cortex_m()
        self._halted()
        # Cortex-M0/M0+ lack the ARMv7-M configurable fault register block.
        cpuid = int.from_bytes(read_bytes(self.probe, 0xE000ED00, 4, 32), "little")
        part = (cpuid >> 4) & 0xFFF
        if part not in (0xC23, 0xC24, 0xC27):
            raise ServiceError("UNSUPPORTED_CORE", "Fault context currently supports Cortex-M3/M4/M7 only")
        reg_names = ("PC", "LR", "SP", "XPSR", "MSP", "PSP")
        items = [contracts.RegisterItem(kind="register", name=n) for n in reg_names]
        names = ("CFSR", "HFSR", "MMFAR", "BFAR")
        items += [contracts.MemoryItem(kind="memory", address=a, size=4, width=32)
                  for a in (0xE000ED28, 0xE000ED2C, 0xE000ED34, 0xE000ED38)]
        evidence = self.read(contracts.ReadRequest(session_id=request.session_id, items=items, consistency="halted"))
        faults = {}
        for name, entry in zip(names, evidence["items"][len(reg_names):]):
            if entry["success"]:
                faults[name] = int.from_bytes(bytes.fromhex(entry["data"]["data_hex"]), "little")
        cfsr = faults.get("CFSR")
        registers = {name: e["data"]["value"] for name, e in zip(reg_names, evidence["items"]) if e["success"]}
        try:
            frame = recover_frame(registers, faults, lambda a, n: read_bytes(self.probe, a, n, 32),
                                  lambda a, n: self._in_memory(a, n, ram_only=True), self._in_memory,
                                  request.frame_address, request.exc_return)
            if frame.get("status") == "decoded":
                frame["locations"] = {n: self._location(frame["registers"][n]) for n in ("PC", "LR")}
        except Exception as exc:
            frame = {"status": "unavailable", "reason": str(exc)}
        self._halted()
        return {"success": evidence["success"], "cpuid": cpuid, "evidence": evidence, "fault_registers": faults,
                "fault_flags": decode_fault_flags(faults),
                "exception_frame": frame,
                "interpretation": {"mmfar_valid": bool(cfsr & (1 << 7)) if cfsr is not None else None,
                                   "bfar_valid": bool(cfsr & (1 << 15)) if cfsr is not None else None,
                                   "note": "Sticky fault status is evidence, not proof of a current root cause"}}

    def capture(self, request):
        if request.action == "diff":
            saved = self.snapshots.get(request.snapshot_id)
            if saved is None or saved["generation"] != self.generation:
                raise ServiceError("STALE_SNAPSHOT", "Snapshot expired after reset, flash, reconnect, or eviction")
            current = self.read(saved["request"])
            changes = []
            for index, (old, new) in enumerate(zip(saved["result"]["items"], current["items"])):
                if not old["success"] or not new["success"]:
                    changes.append({"index": index, "status": "unavailable", "before": old, "after": new})
                elif old["data"] != new["data"]:
                    changes.append({"index": index, "status": "changed", "before": old["data"], "after": new["data"]})
            return {"success": saved["result"]["success"] and current["success"], "changes": changes,
                    "snapshot_timestamp": saved["timestamp"], "current": current}
        reading = contracts.ReadRequest(session_id=request.session_id, items=request.items, consistency=request.consistency)
        if request.action == "snapshot":
            result = self.read(reading)
            key = uuid.uuid4().hex
            if len(self.snapshots) >= 16:
                del self.snapshots[next(iter(self.snapshots))]
            self.snapshots[key] = {"request": reading, "result": copy.deepcopy(result),
                                   "generation": self.generation, "timestamp": utc_now()}
            return {"success": result["success"], "snapshot_id": key, "result": result}
        plans = [self._plan_read(item) for item in request.items]
        if sum(p["bytes"] for p in plans) * request.count > 1024 * 1024:
            raise ServiceError("LIMIT", "Sample payload must not exceed 1 MiB")
        path = Path(request.output_path or f"captures/{uuid.uuid4().hex}.jsonl").resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        total, ok = 0, True
        started = time.monotonic()
        with path.open("x", encoding="utf-8") as stream:
            for index in range(request.count):
                delay = started + index * request.interval_ms / 1000 - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                if time.monotonic() - started > 10:
                    break
                result = self.read(reading)
                stream.write(json.dumps({"index": index, "timestamp": utc_now(), "session_id": self.session_id,
                                         "target": self.target, "generation": self.generation,
                                         "host_elapsed_ms": (time.monotonic() - started) * 1000, "result": result}) + "\n")
                total += 1
                ok = ok and result["success"]
        return {"success": ok and total == request.count, "sample_count": total,
                "complete": total == request.count, "path": str(path), "clock": "host observation, not MCU sample time"}

    def firmware(self, request):
        if request.action == "verify_image":
            if not self.symbols or not self.profile:
                raise ServiceError("PROFILE_REQUIRED", "ELF image comparison requires an ELF and profiled Flash regions")
            self.image_match = {"status": "unknown", "reason": "comparison in progress"}
            result = compare_image(self.symbols, self.profile, lambda address, size: read_bytes(self.probe, address, size))
            result.update(timestamp=utc_now(), generation=self.generation, profile=self.profile_identity)
            self.image_match = copy.deepcopy(result)
            return result
        # Flash algorithms may change execution state. Require an explicit halt
        # first; report the final state instead of promising implicit recovery.
        self._halted()
        if request.action == "backup":
            validate_span(request.address, request.size)
            path = Path(request.output_path).resolve()
            digest, written = hashlib.sha256(), 0
            with path.open("xb") as stream:
                while written < request.size:
                    self._halted()
                    data = read_bytes(self.probe, request.address + written, min(65536, request.size - written))
                    stream.write(data)
                    digest.update(data)
                    written += len(data)
            self._halted()
            return {"success": True, "path": str(path), "size": written, "sha256": digest.hexdigest(),
                    "verification": "single target read; file hash only"}
        if request.action in ("program", "verify"):
            path = Path(request.file_path)
            if path.suffix.lower() != ".bin" or not 0 < path.stat().st_size <= 16 * 1024 * 1024:
                raise ServiceError("INVALID_IMAGE", "Use a nonempty .bin file no larger than 16 MiB")
            data = path.read_bytes()
            validate_span(request.address, len(data))
            if request.action == "verify":
                result = flash.verify_flash(request.address, data.hex())
            else:
                self._remove_owned_breakpoints()
                self._invalidate()
                result = flash.program_flash(request.address, data=data.hex(), verify=True)
            result["image_sha256"] = hashlib.sha256(data).hexdigest()
            result["verification"] = "readback" if result.get("success") else "failed_or_unknown"
            return result
        if request.action == "erase_pages":
            if request.page_size & (request.page_size - 1) or request.address % request.page_size:
                raise ServiceError("ALIGNMENT", "Page size must be power of two and address explicitly page-aligned")
            size = request.page_size * request.count
            validate_span(request.address, size)
            if size > 16 * 1024 * 1024:
                raise ServiceError("LIMIT", "Erase exceeds 16 MiB")
        elif request.confirm_chip != self.target["chip"]:
            raise ServiceError("TARGET_MISMATCH", "confirm_chip must equal the connected chip")
        self._remove_owned_breakpoints()
        self._invalidate()
        if request.action == "erase_pages":
            return flash.erase_sector(request.address, request.count, request.page_size)
        return flash.erase_flash(chip_erase=True)

    def channel(self, request):
        if request.action == "open":
            return rtt.rtt_start(request.buffer_index, block_address=request.block_address)
        if request.action == "close":
            return rtt.rtt_stop()
        if request.action == "status":
            return rtt.rtt_get_status()
        if request.action == "read":
            return rtt.rtt_read(request.buffer_index, request.size, request.timeout_ms)
        return rtt.rtt_write(request.text, request.buffer_index, request.timeout_ms)


service = DebugService()
