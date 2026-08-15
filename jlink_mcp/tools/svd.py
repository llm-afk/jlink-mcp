"""SVD 工具函数.

提供 SVD 相关的 MCP 工具函数，用于查询外设、寄存器信息和读取/解析寄存器值。
"""

from typing import Dict, Any

from ..svd_manager import svd_manager
from ..jlink_manager import jlink_manager
from ..exceptions import JLinkMCPError, JLinkErrorCode
from ..utils import logger


def list_svd_devices() -> Dict[str, Any]:
    """列出所有支持 SVD 的设备.

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - devices: 设备名称列表
        - count: 设备数量
    """
    try:
        if not svd_manager.is_available():
            return {
                "success": False,
                "devices": [],
                "count": 0,
                "error": {
                    "code": JLinkErrorCode.NOT_INITIALIZED.value[0],
                    "description": "SVD 文件不可用",
                    "suggestion": "请检查 jlink_mcp 包内的 tool/SVD_V1.5.6 目录是否存在"
                }
            }
        devices = svd_manager.device_names
        logger.info(f"列出 {len(devices)} 个 SVD 设备")

        return {
            "success": True,
            "devices": devices,
            "count": len(devices)
        }
    except Exception as e:
        logger.error(f"列出 SVD 设备失败: {e}")
        return {
            "success": False,
            "devices": [],
            "count": 0,
            "error": {
                "code": JLinkErrorCode.READ_FAILED.value[0],
                "description": str(e)
            }
        }


def get_svd_peripherals(device_name: str) -> Dict[str, Any]:
    """获取指定设备的所有外设.

    Args:
        device_name: 设备名称（如 FC4150F1MBSxXxxxT1A）

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - device_name: 设备名称
        - peripherals: 外设列表
    """
    try:
        if not svd_manager.is_available():
            raise JLinkMCPError(
                JLinkErrorCode.NOT_INITIALIZED,
                "SVD 文件不可用"
            )

        peripherals = svd_manager.get_peripherals(device_name)
        peripheral_list = []
        for p in peripherals:
            peripheral_list.append({
                "name": p.name,
                "description": p.description,
                "group_name": p.group_name,
                "base_address": f"0x{p.base_address:X}",
                "register_count": len(p.registers)
            })

        logger.info(f"获取设备 {device_name} 的 {len(peripheral_list)} 个外设")

        return {
            "success": True,
            "device_name": device_name,
            "peripherals": peripheral_list,
            "count": len(peripheral_list)
        }
    except JLinkMCPError as e:
        logger.error(f"获取外设失败: {e}")
        return {
            "success": False,
            "device_name": device_name,
            "peripherals": [],
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"获取外设失败: {e}")
        return {
            "success": False,
            "device_name": device_name,
            "peripherals": [],
            "error": {
                "code": JLinkErrorCode.READ_FAILED.value[0],
                "description": str(e)
            }
        }


def get_svd_registers(device_name: str, peripheral_name: str) -> Dict[str, Any]:
    """获取指定外设的所有寄存器.

    Args:
        device_name: 设备名称
        peripheral_name: 外设名称

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - device_name: 设备名称
        - peripheral_name: 外设名称
        - registers: 寄存器列表
    """
    try:
        if not svd_manager.is_available():
            raise JLinkMCPError(
                JLinkErrorCode.NOT_INITIALIZED,
                "SVD 文件不可用"
            )

        peripheral = svd_manager.get_peripheral(device_name, peripheral_name)
        if not peripheral:
            raise JLinkMCPError(
                JLinkErrorCode.NOT_FOUND,
                f"外设 {peripheral_name} 未找到"
            )

        register_list = []
        for r in peripheral.registers:
            register_list.append({
                "name": r.name,
                "description": r.description,
                "address_offset": f"0x{r.address_offset:X}",
                "absolute_address": f"0x{peripheral.base_address + r.address_offset:X}",
                "size": r.size,
                "access": r.access,
                "reset_value": f"0x{r.reset_value:X}" if r.reset_value is not None else None,
                "field_count": len(r.fields)
            })

        logger.info(f"获取外设 {peripheral_name} 的 {len(register_list)} 个寄存器")

        return {
            "success": True,
            "device_name": device_name,
            "peripheral_name": peripheral_name,
            "base_address": f"0x{peripheral.base_address:X}",
            "registers": register_list,
            "count": len(register_list)
        }
    except JLinkMCPError as e:
        logger.error(f"获取寄存器失败: {e}")
        return {
            "success": False,
            "device_name": device_name,
            "peripheral_name": peripheral_name,
            "registers": [],
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"获取寄存器失败: {e}")
        return {
            "success": False,
            "device_name": device_name,
            "peripheral_name": peripheral_name,
            "registers": [],
            "error": {
                "code": JLinkErrorCode.READ_FAILED.value[0],
                "description": str(e)
            }
        }


def read_register_with_fields(
    device_name: str,
    peripheral_name: str,
    register_name: str
) -> Dict[str, Any]:
    """读取寄存器并解析字段（结合 SVD 和实际读取）.

    Args:
        device_name: 设备名称
        peripheral_name: 外设名称
        register_name: 寄存器名称

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - device_name: 设备名称
        - peripheral_name: 外设名称
        - register_name: 寄存器名称
        - absolute_address: 绝对地址
        - raw_value: 原始值
        - hex_value: 十六进制值
        - binary_value: 二进制值
        - fields: 字段解析列表
    """
    try:
        if not svd_manager.is_available():
            raise JLinkMCPError(
                JLinkErrorCode.NOT_INITIALIZED,
                "SVD 文件不可用"
            )

        # 获取寄存器信息
        register = svd_manager.get_register(device_name, peripheral_name, register_name)
        if not register:
            raise JLinkMCPError(
                JLinkErrorCode.NOT_FOUND,
                f"寄存器 {peripheral_name}.{register_name} 未找到"
            )

        # 获取外设基地址
        peripheral = svd_manager.get_peripheral(device_name, peripheral_name)
        if not peripheral:
            raise JLinkMCPError(
                JLinkErrorCode.NOT_FOUND,
                f"外设 {peripheral_name} 未找到"
            )

        # 计算绝对地址
        absolute_address = peripheral.base_address + register.address_offset

        # 从目标设备读取寄存器值
        jlink = jlink_manager.get_jlink()
        data = jlink.memory_read(absolute_address, 4)  # 读取 4 字节
        raw_value = int.from_bytes(data, byteorder='little')

        # 解析字段（使用预计算的 bit_mask，兼容未计算的情况）
        field_results = []
        for field in register.fields:
            # 使用预计算的 mask，如果为 0 则动态计算（向后兼容）
            bit_mask = field.bit_mask if field.bit_mask > 0 else ((1 << field.bit_width) - 1)
            field_value = (raw_value >> field.bit_offset) & bit_mask

            # 查找枚举值
            enum_name = None
            enum_description = None
            for enum in field.enumerated_values:
                if enum.value == field_value:
                    enum_name = enum.name
                    enum_description = enum.description
                    break

            field_results.append({
                "field_name": field.name,
                "field_value": field_value,
                "field_value_hex": f"0x{field_value:X}",
                "field_description": field.description,
                "enum_name": enum_name,
                "enum_description": enum_description,
                "bit_range": f"[{field.bit_offset}:{field.bit_offset + field.bit_width - 1}]",
                "access": field.access
            })

        logger.info(f"读取寄存器 {peripheral_name}.{register_name} = 0x{raw_value:X}")

        return {
            "success": True,
            "device_name": device_name,
            "peripheral_name": peripheral_name,
            "register_name": register_name,
            "register_description": register.description,
            "absolute_address": f"0x{absolute_address:X}",
            "raw_value": raw_value,
            "hex_value": f"0x{raw_value:X}",
            "binary_value": format(raw_value, f'0{register.size}b'),
            "access": register.access,
            "reset_value": f"0x{register.reset_value:X}" if register.reset_value is not None else None,
            "fields": field_results,
            "field_count": len(field_results)
        }
    except JLinkMCPError as e:
        logger.error(f"读取寄存器失败: {e}")
        return {
            "success": False,
            "device_name": device_name,
            "peripheral_name": peripheral_name,
            "register_name": register_name,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"读取寄存器失败: {e}")
        return {
            "success": False,
            "device_name": device_name,
            "peripheral_name": peripheral_name,
            "register_name": register_name,
            "error": {
                "code": JLinkErrorCode.READ_FAILED.value[0],
                "description": str(e),
                "suggestion": "请检查目标是否已连接，外设时钟是否已启用"
            }
        }


def parse_register_value(
    device_name: str,
    peripheral_name: str,
    register_name: str,
    value: int
) -> Dict[str, Any]:
    """解析寄存器值（仅解析，不读取硬件）.

    Args:
        device_name: 设备名称
        peripheral_name: 外设名称
        register_name: 寄存器名称
        value: 寄存器值

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - device_name: 设备名称
        - peripheral_name: 外设名称
        - register_name: 寄存器名称
        - raw_value: 原始值
        - hex_value: 十六进制值
        - binary_value: 二进制值
        - fields: 字段解析列表
    """
    try:
        if not svd_manager.is_available():
            raise JLinkMCPError(
                JLinkErrorCode.NOT_INITIALIZED,
                "SVD 文件不可用"
            )

        # 使用 SVDManager 解析寄存器值
        parsed = svd_manager.parse_register_value(
            device_name, peripheral_name, register_name, value
        )

        if not parsed:
            raise JLinkMCPError(
                JLinkErrorCode.NOT_FOUND,
                f"寄存器 {peripheral_name}.{register_name} 未找到"
            )

        logger.info(f"解析寄存器 {peripheral_name}.{register_name} = 0x{value:X}")

        return {
            "success": True,
            **parsed
        }
    except JLinkMCPError as e:
        logger.error(f"解析寄存器值失败: {e}")
        return {
            "success": False,
            "device_name": device_name,
            "peripheral_name": peripheral_name,
            "register_name": register_name,
            "value": value,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"解析寄存器值失败: {e}")
        return {
            "success": False,
            "device_name": device_name,
            "peripheral_name": peripheral_name,
            "register_name": register_name,
            "value": value,
            "error": {
                "code": JLinkErrorCode.READ_FAILED.value[0],
                "description": str(e)
            }
        }


def read_register_by_address(
    address: int,
    width: int = 32,
    device_name: str | None = None
) -> Dict[str, Any]:
    """按地址直接读取寄存器值（不依赖 SVD）.

    无需 SVD 文件即可按绝对地址读取寄存器值。
    如果同时提供 device_name 且 SVD 可用，会尝试反查该地址对应的
    外设/寄存器名称并解析字段（best-effort，反查失败不影响读取）。

    Args:
        address: 寄存器绝对地址
        width: 访问宽度（8/16/32位，默认 32）
        device_name: 可选，用于反查寄存器名和字段解析

    Returns:
        - success, address, raw_value, hex_value, binary_value, width
        - peripheral_name/register_name/fields（仅当反查成功时附加）
    """
    try:
        if width not in (8, 16, 32):
            raise JLinkMCPError(
                JLinkErrorCode.INVALID_PARAMETER,
                f"宽度必须是 8/16/32: {width}"
            )

        num_bytes = width // 8

        jlink = jlink_manager.get_jlink()

        # 目标运行中时先暂停，避免 -3 读取失败
        try:
            if hasattr(jlink, 'halted') and not jlink.halted():
                logger.warning(f"目标正在运行，暂停后读取地址 {address:#x}")
                jlink.halt()
        except Exception:
            pass

        data = jlink.memory_read(address, num_bytes)
        raw_value = int.from_bytes(data, byteorder='little')

        result = {
            "success": True,
            "address": f"0x{address:X}",
            "raw_value": raw_value,
            "hex_value": f"0x{raw_value:X}",
            "binary_value": format(raw_value, f'0{width}b'),
            "width": width
        }

        # 可选：通过 SVD 反查寄存器名和字段
        if device_name and svd_manager.is_available():
            found = svd_manager.find_register_by_address(device_name, address)
            if found:
                peripheral, register = found
                parsed = svd_manager.parse_register_value(
                    device_name, peripheral.name, register.name, raw_value
                )
                if parsed:
                    result["peripheral_name"] = peripheral.name
                    result["register_name"] = register.name
                    result["register_description"] = register.description
                    result["fields"] = parsed.get("fields", [])
                    result["field_count"] = len(result["fields"])

        logger.info(f"按地址读取寄存器 {address:#x} = {raw_value:#x}")
        return result
    except JLinkMCPError as e:
        logger.error(f"按地址读取寄存器失败: {e}")
        return {
            "success": False,
            "address": f"0x{address:X}",
            "raw_value": None,
            "hex_value": None,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"按地址读取寄存器失败: {e}")
        return {
            "success": False,
            "address": f"0x{address:X}",
            "raw_value": None,
            "hex_value": None,
            "error": {
                "code": JLinkErrorCode.READ_FAILED.value[0],
                "description": str(e),
                "suggestion": "请检查地址是否有效，目标是否已暂停"
            }
        }


def write_register_by_address(
    address: int,
    value: int,
    width: int = 32
) -> Dict[str, Any]:
    """按地址直接写入寄存器值（不依赖 SVD）.

    无需 SVD 文件即可按绝对地址写入寄存器值。

    Args:
        address: 寄存器绝对地址
        value: 要写入的值
        width: 访问宽度（8/16/32位，默认 32）

    Returns:
        - success, address, value, hex_value, width
    """
    try:
        if width not in (8, 16, 32):
            raise JLinkMCPError(
                JLinkErrorCode.INVALID_PARAMETER,
                f"宽度必须是 8/16/32: {width}"
            )
        if value < 0 or value >= (1 << width):
            raise JLinkMCPError(
                JLinkErrorCode.INVALID_PARAMETER,
                f"值 {value} 超出 {width} 位范围 [0, {(1 << width) - 1}]"
            )

        num_bytes = width // 8
        data = value.to_bytes(num_bytes, byteorder='little')

        jlink = jlink_manager.get_jlink()
        jlink.memory_write(address, data)

        logger.info(f"按地址写入寄存器 {address:#x} = {value:#x}")

        return {
            "success": True,
            "address": f"0x{address:X}",
            "value": value,
            "hex_value": f"0x{value:X}",
            "width": width,
            "message": f"成功写入 {value:#x} 到地址 {address:#x}"
        }
    except JLinkMCPError as e:
        logger.error(f"按地址写入寄存器失败: {e}")
        return {
            "success": False,
            "address": f"0x{address:X}",
            "value": value,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"按地址写入寄存器失败: {e}")
        return {
            "success": False,
            "address": f"0x{address:X}",
            "value": value,
            "error": {
                "code": JLinkErrorCode.WRITE_FAILED.value[0],
                "description": str(e),
                "suggestion": "请检查地址是否有效，寄存器是否可写"
            }
        }