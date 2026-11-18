"""Strict, task-oriented MCP contracts. Unsupported options are never ignored."""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

Address = Annotated[int, Field(ge=0, le=0xFFFFFFFF)]
Name = Annotated[str, Field(min_length=1, max_length=256)]
Size = Annotated[int, Field(gt=0, le=65536)]
Width = Literal[8, 16, 32]
Speed = Union[Literal["auto"], Annotated[int, Field(ge=5, le=50000)]]


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
    verify_image: bool = Field(default=False, description="Compare immutable Flash to the profile ELF during open. Use for typed variables or HSS; check image_match.status.")
    jlink_path: str | None = None
    speed_khz: Speed | None = Field(default=None, description="Interface speed in kHz or auto; inherits profile when omitted. Fixed speed requires identical probe readback.")

    @model_validator(mode="after")
    def target_required(self):
        if not self.profile_path and (not self.chip or not self.serial_number):
            raise ValueError("Provide profile_path or both chip and serial_number")
        return self


class SessionStatus(Request):
    action: Literal["status"]
    compact: bool = Field(default=False, description="Return live connection and cached image evidence without profile, capability or RTT details.")


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
    name: Name = Field(description="Static DWARF global.member[index], including named union members; no calls or pointer dereference")
    require_match: bool = Field(default=True, description="Require matched ELF read-only image evidence in this session")


ReadItem = Annotated[Union[MemoryItem, RegisterItem, PeripheralItem, SymbolItem, VariableItem], Field(discriminator="kind")]


def _expand_variable_name(item):
    return {"kind": "variable", "name": item} if isinstance(item, str) else item


ItemInput = Annotated[ReadItem, BeforeValidator(_expand_variable_name, json_schema_input_type=Union[Name, ReadItem])]
Items = Annotated[list[ItemInput], Field(min_length=1, max_length=64)]


class ReadBatch(Bound):
    action: Literal["read"]
    items: Items
    consistency: Literal["live", "halted"] = "live"
    compact: bool = Field(default=False, description="Keep raw bytes and values, omit per-item DWARF layouts. Sample records layouts once.")


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


ScalarValue = Union[bool, int, float, Annotated[str, Field(min_length=1, max_length=256)]]


class VariableWrite(Request):
    kind: Literal["variable"]
    name: Name = Field(description="Explicit static global/member/index, including named union members; matched ELF and halted RAM required")
    value: Union[ScalarValue, Annotated[list[ScalarValue], Field(min_length=1, max_length=4096)]] = Field(
        description="Scalar or exact-length 1D scalar array; enum accepts an exact name or integer; no struct/union objects")


WriteItem = Annotated[Union[MemoryWrite, RegisterWrite, PeripheralWrite, VariableWrite], Field(discriminator="kind")]


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


class ObserveStop(Bound):
    read_on_stop: Items | None = Field(default=None, description="Read these items when a stop is observed. See stop.read for the compact halted batch and its own success flag.")


class WaitControl(ObserveStop):
    action: Literal["wait"]
    timeout_ms: Annotated[int, Field(ge=1, le=30000)] = 1000


class RunUntilControl(ObserveStop):
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


class BreakpointAdd(Bound):
    action: Literal["breakpoint_set"]
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
    action: Literal["breakpoint_remove"]
    breakpoint_id: Name


class BreakpointList(Bound):
    action: Literal["breakpoint_list"]


ControlRequest = Annotated[Union[RunControl, ResetControl, WaitControl, RunUntilControl,
                                BreakpointAdd, BreakpointRemove, BreakpointList], Field(discriminator="action")]


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


class FindSymbol(Bound):
    action: Literal["find_symbol"]
    query: Name = Field(description="Case-insensitive literal substring of an ELF symbol name.")
    kind: Literal["object", "function"] | None = None
    limit: Annotated[int, Field(ge=1, le=32)] = 8


class InspectVariable(Bound):
    action: Literal["variable"]
    name: Name


class InspectPreflight(Bound):
    action: Literal["preflight"]


InspectRequest = Annotated[Union[InspectFault, InspectContext, InspectSvd, InspectSymbol, FindSymbol, InspectVariable, InspectPreflight], Field(discriminator="action")]


class Snapshot(ReadBatch):
    action: Literal["snapshot"]


class Diff(Bound):
    action: Literal["diff"]
    snapshot_id: Name
    compact: bool = Field(default=False, description="Return only changed or unavailable items.")


class Sample(ReadBatch):
    action: Literal["sample"]
    count: Annotated[int, Field(ge=1, le=128)] = 10
    interval_ms: Annotated[int, Field(ge=10, le=1000)] = 100
    output_path: str | None = None

    @model_validator(mode="after")
    def bounded_duration(self):
        if self.count * self.interval_ms > 10000:
            raise ValueError("Requested sampling window must not exceed 10000 ms")
        return self


class HssCapture(Bound):
    action: Literal["hss"]
    items: Annotated[list[Name], Field(min_length=1, max_length=32,
        description="Scalar ELF variable paths in desired CSV column order")]
    rate_hz: Annotated[int, Field(ge=1, le=100000)] = 1000
    count: Annotated[int, Field(ge=2, le=100000)] | None = None
    duration_ms: Annotated[int, Field(ge=1, le=10000)] | None = Field(
        default=None, description="Capture window in ms instead of count. Default is one second, at least two frames.")
    output_path: str | None = None

    @property
    def period_us(self):
        return (1000000 + self.rate_hz - 1) // self.rate_hz

    @property
    def sample_count(self):
        if self.count is not None:
            return self.count
        window_us = (self.duration_ms or 1000) * 1000
        frames = (window_us + self.period_us - 1) // self.period_us
        return frames if self.duration_ms is not None else max(2, frames)

    @model_validator(mode="after")
    def bounded_capture(self):
        if self.duration_ms is not None and self.count is not None:
            raise ValueError("Use either duration_ms or count")
        if not 2 <= self.sample_count <= 100000 or (self.sample_count - 1) * self.period_us >= 10000000:
            raise ValueError("Choose a window with 2–100000 frames within 10 seconds")
        if len(set(self.items)) != len(self.items):
            raise ValueError("HSS column names must be unique")
        return self


ReadRequest = Annotated[Union[ReadBatch, Snapshot, Diff, Sample, HssCapture], Field(discriminator="action")]


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
    format: Literal["text_hex", "text", "base64"] = "text_hex"


class ChannelCapture(Bound):
    action: Literal["capture"]
    buffer_index: Annotated[int, Field(ge=0, le=255)] | None = None
    size: Annotated[int, Field(ge=1, le=16 * 1024 * 1024)]
    timeout_ms: Annotated[int, Field(ge=1, le=30000)]
    output_path: Annotated[str, Field(min_length=1)]


class ChannelWrite(Bound):
    action: Literal["write"]
    buffer_index: Annotated[int, Field(ge=0, le=255)] | None = None
    text: Annotated[str, Field(max_length=65536)] | None = None
    data_b64: Annotated[str, Field(min_length=4, max_length=87384)] | None = None
    timeout_ms: Annotated[int, Field(ge=0, le=30000)] = 1000

    @model_validator(mode="after")
    def one_payload(self):
        if (self.text is None) == (self.data_b64 is None):
            raise ValueError("Provide exactly one of text or data_b64")
        return self


class ChannelState(Bound):
    action: Literal["status", "close"]


class ChannelExchange(ChannelWrite):
    action: Literal["exchange"]
    reply_size: Size = Field(description="Consume exactly this many bytes from the same RTT up-channel index")
    timeout_ms: Annotated[int, Field(ge=1, le=30000)] = 1000


ChannelRequest = Annotated[Union[ChannelOpen, ChannelRead, ChannelCapture, ChannelWrite, ChannelExchange, ChannelState], Field(discriminator="action")]


# The worker and service use the same public request registry.
TOOL_REQUESTS = {"session": SessionRequest, "read": ReadRequest, "write": WriteRequest,
                 "control": ControlRequest, "inspect": InspectRequest,
                 "firmware": FirmwareRequest, "channel": ChannelRequest}
