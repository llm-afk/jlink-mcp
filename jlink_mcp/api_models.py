"""Strict, task-oriented MCP contracts. Unsupported options are never ignored."""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

Address = Annotated[int, Field(ge=0, le=0xFFFFFFFF)]
Name = Annotated[str, Field(min_length=1, max_length=256)]
Size = Annotated[int, Field(gt=0, le=65536)]
Width = Literal[8, 16, 32]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Bound(Request):
    session_id: Name


class OpenSession(Request):
    action: Literal["open"]
    profile_path: str | None = None
    chip: Name | None = None
    serial_number: Name | None = None
    interface: Literal["SWD", "JTAG"] | None = None
    architecture: Literal["cortex-m", "unknown"] | None = None
    svd_device: Name | None = None
    elf_path: str | None = None
    jlink_path: str | None = None

    @model_validator(mode="after")
    def target_required(self):
        if not self.profile_path and (not self.chip or not self.serial_number):
            raise ValueError("Provide profile_path or both chip and serial_number")
        return self


class SessionStatus(Request):
    action: Literal["status"]


class CloseSession(Bound):
    action: Literal["close"]


SessionRequest = Annotated[Union[OpenSession, SessionStatus, CloseSession], Field(discriminator="action")]


class MemoryItem(Request):
    kind: Literal["memory"]
    address: Address
    size: Size
    width: Width = 8


class RegisterItem(Request):
    kind: Literal["register"]
    name: Name


class PeripheralItem(Request):
    kind: Literal["peripheral"]
    name: Name = Field(description="SVD peripheral.register; uses the session SVD device")
    allow_side_effects: bool = False


class SymbolItem(Request):
    kind: Literal["symbol"]
    name: Name = Field(description="Exact ELF object symbol; returns raw bytes, not DWARF expression evaluation")


class VariableItem(Request):
    kind: Literal["variable"]
    name: Name = Field(description="Static DWARF global.member[index]; no calls or pointer dereference")
    require_match: bool = Field(default=True, description="Require matched ELF read-only image evidence in this session")


ReadItem = Annotated[Union[MemoryItem, RegisterItem, PeripheralItem, SymbolItem, VariableItem], Field(discriminator="kind")]
Items = Annotated[list[ReadItem], Field(min_length=1, max_length=64)]


class ReadRequest(Bound):
    items: Items
    consistency: Literal["live", "halted"] = "live"


class MemoryWrite(Request):
    kind: Literal["memory"]
    address: Address
    data_hex: Annotated[str, Field(min_length=2, max_length=131072)]
    width: Width = 8


class RegisterWrite(Request):
    kind: Literal["register"]
    name: Name
    value: Address


class PeripheralWrite(Request):
    kind: Literal["peripheral"]
    name: Name
    value: Address


WriteItem = Annotated[Union[MemoryWrite, RegisterWrite, PeripheralWrite], Field(discriminator="kind")]


class WriteRequest(Bound):
    item: WriteItem
    verify: bool = Field(default=False, description="Explicit readback; may have peripheral read side effects")
    consistency: Literal["live", "halted"] = "halted"


class RunControl(Bound):
    action: Literal["halt", "resume", "step"]
    timeout_ms: Annotated[int, Field(ge=1, le=30000)] = 1000


class ResetControl(Bound):
    action: Literal["reset"]
    mode: Literal["normal", "halt", "core"] = "halt"


class WaitControl(Bound):
    action: Literal["wait"]
    timeout_ms: Annotated[int, Field(ge=1, le=30000)] = 1000


class RunUntilControl(Bound):
    action: Literal["run_until"]
    address: Address | None = None
    symbol: Name | None = None
    timeout_ms: Annotated[int, Field(ge=1, le=30000)] = 1000
    on_timeout: Literal["halt", "running"] = "halt"

    @model_validator(mode="after")
    def one_location(self):
        if (self.address is None) == (self.symbol is None):
            raise ValueError("Provide exactly one of address or symbol")
        return self


ControlRequest = Annotated[Union[RunControl, ResetControl, WaitControl, RunUntilControl], Field(discriminator="action")]


class BreakpointAdd(Bound):
    action: Literal["set"]
    kind: Literal["execute", "read", "write", "access"] = "execute"
    address: Address | None = None
    symbol: Name | None = None
    variable: Name | None = None
    size: Literal[1, 2, 4] | None = None

    @model_validator(mode="after")
    def one_location(self):
        if sum(v is not None for v in (self.address, self.symbol, self.variable)) != 1:
            raise ValueError("Provide exactly one of address, symbol or variable")
        if self.kind == "execute" and (self.variable is not None or self.size is not None):
            raise ValueError("Execution breakpoints take address or function symbol, not variable/size")
        if self.kind != "execute" and (self.symbol is not None or (self.address is not None and self.size is None)):
            raise ValueError("Data watchpoints take variable or address plus size")
        return self


class BreakpointRemove(Bound):
    action: Literal["remove"]
    breakpoint_id: Name


class BreakpointList(Bound):
    action: Literal["list"]


BreakpointRequest = Annotated[Union[BreakpointAdd, BreakpointRemove, BreakpointList], Field(discriminator="action")]


class InspectFault(Bound):
    action: Literal["fault"]
    frame_address: Address | None = None
    exc_return: Address | None = None

    @model_validator(mode="after")
    def frame_pair(self):
        if (self.frame_address is None) != (self.exc_return is None):
            raise ValueError("Provide frame_address and exc_return together, or neither")
        return self


class InspectContext(Bound):
    action: Literal["context"]
    address: Address | None = None
    instructions: Annotated[int, Field(ge=0, le=32)] = 8


class InspectSvd(Bound):
    action: Literal["svd"]
    peripheral: Name | None = None


class InspectSymbol(Bound):
    action: Literal["symbol"]
    name: Name


class InspectVariable(Bound):
    action: Literal["variable"]
    name: Name


class InspectPreflight(Bound):
    action: Literal["preflight"]


InspectRequest = Annotated[Union[InspectFault, InspectContext, InspectSvd, InspectSymbol, InspectVariable, InspectPreflight], Field(discriminator="action")]


class Snapshot(ReadRequest):
    action: Literal["snapshot"]


class Diff(Bound):
    action: Literal["diff"]
    snapshot_id: Name


class Sample(ReadRequest):
    action: Literal["sample"]
    count: Annotated[int, Field(ge=1, le=128)] = 10
    interval_ms: Annotated[int, Field(ge=10, le=1000)] = 100
    output_path: str | None = None

    @model_validator(mode="after")
    def bounded_duration(self):
        if self.count * self.interval_ms > 10000:
            raise ValueError("Requested sampling window must not exceed 10000 ms")
        return self


CaptureRequest = Annotated[Union[Snapshot, Diff, Sample], Field(discriminator="action")]


class FirmwareImage(Bound):
    action: Literal["program", "verify"]
    address: Address
    file_path: str = Field(description="Raw BIN only; no automatic ELF/HEX conversion")


class FirmwareBackup(Bound):
    action: Literal["backup"]
    address: Address
    size: Annotated[int, Field(gt=0, le=16 * 1024 * 1024)]
    output_path: str


class FirmwareErasePages(Bound):
    action: Literal["erase_pages"]
    address: Address
    page_size: Annotated[int, Field(gt=0, le=16 * 1024 * 1024)]
    count: Annotated[int, Field(gt=0, le=16384)] = 1


class FirmwareEraseChip(Bound):
    action: Literal["erase_chip"]
    confirm_chip: Name = Field(description="Must exactly equal session chip; erases bootloader and app")


class FirmwareVerifyImage(Bound):
    action: Literal["verify_image"]


FirmwareRequest = Annotated[Union[FirmwareImage, FirmwareBackup, FirmwareErasePages, FirmwareEraseChip, FirmwareVerifyImage], Field(discriminator="action")]


class ChannelOpen(Bound):
    action: Literal["open"]
    buffer_index: Annotated[int, Field(ge=0, le=255)] = 0
    block_address: Address | None = None


class ChannelRead(Bound):
    action: Literal["read"]
    buffer_index: Annotated[int, Field(ge=0, le=255)] | None = None
    size: Size = 1024
    timeout_ms: Annotated[int, Field(ge=0, le=30000)] = 0


class ChannelWrite(Bound):
    action: Literal["write"]
    buffer_index: Annotated[int, Field(ge=0, le=255)] | None = None
    text: Annotated[str, Field(max_length=65536)]
    timeout_ms: Annotated[int, Field(ge=0, le=30000)] = 1000


class ChannelState(Bound):
    action: Literal["status", "close"]


ChannelRequest = Annotated[Union[ChannelOpen, ChannelRead, ChannelWrite, ChannelState], Field(discriminator="action")]
