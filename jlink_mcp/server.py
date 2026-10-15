"""Eight task-oriented MCP tools; hardware backends stay private."""
from contextlib import asynccontextmanager

import anyio
from mcp.server.fastmcp import FastMCP

from .api_models import (SessionRequest, ReadRequest, WriteRequest, ControlRequest,
                         InspectRequest, FirmwareRequest, ChannelRequest)
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

    profile_path can supply chip, architecture, paths, memory regions and speed_khz.
    Fixed speed requires matching probe readback; status reports the current speed.
    Optional ELF provides raw symbols and bounded static DWARF types; target
    matching is unknown until firmware verify_image. Loading never writes debug bits.
    Use the returned session_id in all subsequent target operations.
    """
    return await call("session", request)


@mcp.tool()
async def read(request: ReadRequest) -> dict:
    """Read/snapshot/diff/sample memory, registers, SVD peripherals and ELF variables.

    action selects read/snapshot/diff/sample. Each batch is <=64KB, sequential,
    never atomic. live never halts; CPU registers require explicit halt + halted.
    Variables support global.member[index], including named union members,
    profiled bounds and matched image by default. Whole unions return opaque
    bytes; require_match=false marks uncertain interpretation.
    At most 16 snapshots; expire on reset/Flash/reconnect or eviction. Sampling writes an
    exclusive JSONL file: <=128 samples, 1MiB raw payload, 10s requested window.
    """
    return await call("read", request)


@mcp.tool()
async def write(request: WriteRequest) -> dict:
    """Write memory, a register/peripheral, or a typed ELF variable with readback.

    Never auto-halts or retries. Verification can trigger peripheral read side
    effects. Typed writes require halted consistency, matched image and profiled
    RAM; select a scalar or fixed 1D scalar array, including named union members.
    const/atomic and aggregate writes are rejected. Submitted writes invalidate
    image matching; reverify before further typed access. Use firmware for Flash.
    """
    return await call("write", request)


@mcp.tool()
async def control(request: ControlRequest) -> dict:
    """Halt/resume/reset/step/wait/run_until and owned hardware break/watchpoints.

    breakpoint_set/remove/list manage only this session's handles. Set/remove
    require halted Cortex-M; kind=execute/read/write/access. Data locations take
    aligned address+size (1/2/4 bytes) or a matched variable. No Flash fallback.
    wait only observes. run_until requires halt, cleans its temporary breakpoint,
    and selects halt/running on_timeout. Stops report reasons and PC/LR/SP/XPSR.
    Function symbols require matched image evidence. No automatic retry/reset.
    Reset invalidates snapshots/RTT and removes owned breakpoints.
    """
    return await call("control", request)


@mcp.tool()
async def inspect(request: InspectRequest) -> dict:
    """Inspect symbols/types/SVD/preflight, halted context or Cortex-M fault evidence.

    variable resolves explicit global/member/index paths, including named union
    members, without reading the target. context maps PC/LR to ELF source ranges
    and disassembles bounded target bytes.
    fault recovers a bounded exception frame at handler entry or caller-supplied
    frame_address+exc_return, with profile RAM bounds. No guessed call stack.
    """
    return await call("inspect", request)


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
    return ("discover -> session open -> read action=read/live; read also owns snapshot/diff/sample. "
            "control owns breakpoint_set/remove/list and must halt explicitly for CPU registers, "
            "stepping or Flash modification. Profile sessions may verify_image live before typed reads. Every target request carries session_id. Inspect capability "
            "limits in session status. Never infer completion from driver submission. "
            "ELF identity is not proof of the flashed image. See README and docs/MIGRATION.md.")


def main():
    logger.info("Starting compact J-Link MCP API 0.6 (stdio)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
