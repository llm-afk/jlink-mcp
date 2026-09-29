"""Declarative JSON project profiles. Loading one never touches the target."""
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Region(ProfileModel):
    name: str = Field(min_length=1)
    address: int = Field(ge=0, le=0xFFFFFFFF)
    size: int = Field(gt=0, le=0x100000000)
    kind: Literal["flash", "ram"]
    mutable: bool = False

    @model_validator(mode="after")
    def bounds(self):
        if self.address + self.size > 0x100000000:
            raise ValueError("Region exceeds the 32-bit address space")
        return self


class DebugCheck(ProfileModel):
    name: str = Field(min_length=1)
    address: int = Field(ge=0, le=0xFFFFFFFC, multiple_of=4)
    mask: int = Field(gt=0, le=0xFFFFFFFF)
    expected: int = Field(ge=0, le=0xFFFFFFFF)
    description: str

    @model_validator(mode="after")
    def masked_value(self):
        if self.expected & ~self.mask:
            raise ValueError("Expected value contains bits outside mask")
        return self


class ProjectProfile(ProfileModel):
    version: Literal[1] = 1
    name: str = Field(min_length=1)
    chip: str = Field(min_length=1)
    serial_number: str | None = None
    interface: Literal["SWD", "JTAG"] = "SWD"
    architecture: Literal["cortex-m", "unknown"] = "unknown"
    svd_device: str | None = None
    elf_path: str | None = None
    jlink_path: str | None = None
    regions: Annotated[list[Region], Field(max_length=64)] = Field(default_factory=list)
    debug_checks: Annotated[list[DebugCheck], Field(max_length=16)] = Field(default_factory=list)

    @model_validator(mode="after")
    def nonoverlapping(self):
        ordered = sorted(self.regions, key=lambda region: region.address)
        if len({r.name for r in ordered}) != len(ordered):
            raise ValueError("Region names must be unique")
        if any(a.address + a.size > b.address for a, b in zip(ordered, ordered[1:])):
            raise ValueError("Profile regions overlap")
        return self


def load_profile(path):
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > 65536:
        raise ValueError("Profile exceeds 64KB")
    content = path.read_bytes()
    profile = ProjectProfile.model_validate(json.loads(content.decode("utf-8-sig")))
    for field in ("elf_path", "jlink_path"):
        value = getattr(profile, field)
        if value:
            resolved = Path(value)
            setattr(profile, field, str((path.parent / resolved).resolve()))
    return profile, {"path": str(path), "sha256": hashlib.sha256(content).hexdigest(), "name": profile.name}
