"""Eight task-oriented MCP tools; hardware backends stay private."""
from contextlib import asynccontextmanager

import anyio
from mcp.server.fastmcp import FastMCP

from . import __version__
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
# FastMCP 1.x otherwise advertises the MCP SDK's version during initialize.
mcp._mcp_server.version = __version__


async def call(name, request=None):
    # Cancellation must not release ownership while the worker still runs.
    return await anyio.to_thread.run_sync(worker.invoke, name, request)


@mcp.tool()
async def discover() -> dict:
    """Enumerate probes, SVD profiles and capabilities without connecting to a target."""
    return await call("discover")


@mcp.tool()
async def session(request: SessionRequest) -> dict:
    """Open with a chip and probe serial, optionally supplied by a project profile.
    Use session_id for target calls. Status reports server_version, connection,
    speed and ELF match. Run firmware verify_image before typed access.
    """
    return await call("session", request)


@mcp.tool()
async def read(request: ReadRequest) -> dict:
    """Read/snapshot/diff/sample up to 64 KiB per sequential batch. CPU registers require halt.
    Typed reads use matched ELF global.member[index] paths. Use compact=true for
    polling, changes-only diffs or sampling with one copy of the types in JSONL.
    Snapshots expire on reset, Flash changes, reconnect or eviction (16 slots).
    Sample creates a new file, up to 128 samples, 1 MiB raw and 10 seconds.
    """
    return await call("read", request)


@mcp.tool()
async def write(request: WriteRequest) -> dict:
    """Write one memory span, register, peripheral or typed RAM variable. Use firmware for Flash.
    Typed writes require a matched ELF and halted CPU. A successful verified typed
    RAM write preserves the earlier Flash match. Other submissions invalidate it.
    Readback is optional and can affect peripherals. Writes never auto-halt or retry.
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
    """RTT text and binary I/O. Read as text_hex (default), text or base64.
    Write text or data_b64. bytes_written counts accepted bytes. Capture drains
    up to size raw bytes to a new file within timeout_ms and preserves partial data.
    Status reports buffer and host overflow counters from the driver when available.
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
    logger.info("Starting J-Link MCP %s (stdio)", __version__)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
