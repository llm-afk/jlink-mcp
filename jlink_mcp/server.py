"""Ten task-oriented MCP tools; hardware backends stay private."""
from contextlib import asynccontextmanager

import anyio
from mcp.server.fastmcp import FastMCP

from .api_models import (SessionRequest, ReadRequest, WriteRequest, ControlRequest,
                         BreakpointRequest, InspectRequest, CaptureRequest,
                         FirmwareRequest, ChannelRequest)
from .worker import worker
from .utils import logger


@asynccontextmanager
async def lifespan(_):
    try:
        yield {}
    finally:
        await anyio.to_thread.run_sync(worker.close)


mcp = FastMCP("jlink-mcp-server", lifespan=lifespan)


async def call(name, request=None):
    # Cancellation must not release ownership while the worker still runs.
    return await anyio.to_thread.run_sync(worker.invoke, name, request)


@mcp.tool()
async def discover() -> dict:
    """Enumerate probes, SVD profiles and capabilities without connecting to a target."""
    return await call("discover")


@mcp.tool()
async def session(request: SessionRequest) -> dict:
    """Open/status/close a single probe session. Open requires explicit chip and serial.

    profile_path can supply chip, architecture, paths and memory regions.
    Optional ELF provides raw symbols and bounded static DWARF types; target
    matching is unknown until firmware verify_image. Loading never writes debug bits.
    Use the returned session_id in all subsequent target operations.
    """
    return await call("session", request)


@mcp.tool()
async def read(request: ReadRequest) -> dict:
    """Batch read memory, registers, SVD peripherals, ELF symbols or typed variables (64KB).

    live never halts and rejects CPU register reads. halted requires a prior
    explicit control halt. Results are sequential, not atomic, with item errors.
    Typed variables support global.member[index] and require a profile plus a
    matched image by default; require_match=false explicitly permits uncertain interpretation.
    """
    return await call("read", request)


@mcp.tool()
async def write(request: WriteRequest) -> dict:
    """Write one memory span or register, with optional explicit readback.

    Never auto-halts or retries. Verification can trigger peripheral read side
    effects. For firmware use firmware, not raw memory writes.
    """
    return await call("write", request)


@mcp.tool()
async def control(request: ControlRequest) -> dict:
    """Halt/resume/reset/step, wait for stop, or run_until address/function.

    wait never resumes or halts. run_until requires halted Cortex-M and cleans
    its temporary hardware breakpoint; on_timeout explicitly selects halt/running.
    Stops report native reasons and PC/LR/SP/XPSR. No automatic retry/reset.
    Symbol targets require matched image evidence. Reset invalidates captures/RTT.
    """
    return await call("control", request)


@mcp.tool()
async def breakpoint(request: BreakpointRequest) -> dict:
    """Set/list/remove owned hardware execution breakpoints or data watchpoints.

    Set/remove require halted Cortex-M. kind is execute/read/write/access;
    data locations take aligned address+size (1/2/4 bytes) or a matched DWARF variable.
    Function symbols require matched image evidence. No software Flash fallback.
    """
    return await call("breakpoint", request)


@mcp.tool()
async def inspect(request: InspectRequest) -> dict:
    """Inspect symbols/types/SVD/preflight, halted context or Cortex-M fault evidence.

    context maps PC/LR to ELF source ranges and disassembles bounded target bytes.
    fault recovers a bounded exception frame at handler entry or caller-supplied
    frame_address+exc_return, with profile RAM bounds. No guessed call stack.
    """
    return await call("inspect", request)


@mcp.tool()
async def capture(request: CaptureRequest) -> dict:
    """Snapshot/diff or bounded host sampling into an exclusive JSONL file.

    At most 16 snapshots, 128 samples, 1MiB raw sample payload, 10s requested
    window. Driver latency is additional. No implicit halt or background worker.
    Snapshots expire on reset, firmware modification, reconnect or eviction.
    """
    return await call("capture", request)


@mcp.tool()
async def firmware(request: FirmwareRequest) -> dict:
    """Verify ELF read-only image, program/verify BIN, backup or explicitly erase.

    verify_image preserves CPU state and compares immutable profiled Flash.
    Other actions require an explicit halt. Program always verifies. Backups never overwrite;
    failed backups may leave incomplete files. Chip erase requires confirm_chip.
    Page size must match hardware. No automatic reset, resume or retry.
    """
    return await call("firmware", request)


@mcp.tool()
async def channel(request: ChannelRequest) -> dict:
    """RTT open/status/read/write/close with bounded waits and actual byte counts.

    Writes retry only unsent suffixes internally; never resend an entire partial
    command. Reset, firmware modification and disconnect invalidate RTT.
    """
    return await call("channel", request)


@mcp.resource("debug://guide")
def guide() -> str:
    """Usage guidance is a resource, not an extra hardware tool."""
    return ("discover -> session open -> read/live; control halt explicitly for CPU registers, "
            "stepping or Flash modification. Profile sessions may verify_image live before typed reads. Every target request carries session_id. Inspect capability "
            "limits in session status. Never infer completion from driver submission. "
            "ELF identity is not proof of the flashed image. See README and docs/MIGRATION.md.")


def main():
    logger.info("Starting compact J-Link MCP API 0.5 (stdio)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
