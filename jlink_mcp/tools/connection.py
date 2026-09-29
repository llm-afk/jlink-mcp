"""连接管理工具函数."""

from typing import List, Dict, Any

from ..jlink_manager import jlink_manager
from ..models.device import TargetInterface
from ..utils import logger


def list_jlink_devices() -> List[Dict[str, Any]]:
    """列出所有连接的 JLink 设备.

    返回系统中所有已连接的 JLink 调试器列表。
    每个设备包含序列号、产品名称和连接类型。

    Returns:
        设备信息列表，每个设备包含:
        - serial_number: 设备序列号
        - product_name: 产品名称
        - firmware_version: 固件版本
        - connection_type: 连接类型（USB/ETH）
    """
    devices = jlink_manager.enumerate_devices()
    return [device.model_dump() for device in devices]


def connect_device(serial_number: str | None = None, interface: str | None = None, chip_name: str | None = None, jlink_path: str | None = None) -> Dict[str, Any]:
    """连接到 JLink 设备.

    连接到指定的 JLink 调试器。如果不指定序列号，则连接第一个可用设备。
    连接成功后，可以执行其他操作如读写内存、控制调试等。

    Args:
        serial_number: 设备序列号（可选，None 则连接第一个设备）
        interface: 目标接口类型，支持 "SWD" 或 "JTAG"（可选，默认 SWD）
        chip_name: 目标芯片名称（可选，支持缩写自动匹配，如 FC7300F4MDD）
        jlink_path: 指定 JLink 安装目录或 JLink_x64.dll 文件路径（可选，
            如 D:\\Program Files\\SEGGER\\JLink_V942）；None 则自动选择最新版本

    Returns:
        连接结果，包含:
        - success: 是否成功
        - serial_number: 连接的设备序列号
        - message: 状态信息
    """
    try:
        # Public session configuration is resolved before calling this adapter.
        if interface is None:
            interface = "SWD"

        interface_enum = TargetInterface(interface.upper())
        jlink_manager.connect(serial_number, interface_enum, chip_name, jlink_path)
        status = jlink_manager.get_connection_status()

        logger.info(f"成功连接到设备: {status.device_serial}")
        return {
            "success": True,
            "serial_number": status.device_serial,
            "message": f"成功连接到设备 {status.device_serial}，接口: {interface}"
        }
    except Exception as e:
        logger.error(f"连接失败: {e}")
        error_msg = str(e)

        # 检查是否是设备不支持的错误
        if "unsupported device" in error_msg.lower() or "not found" in error_msg.lower():
            from ..device_patch_manager import device_patch_manager
            if chip_name:
                # 提供设备名称建议
                suggestions = device_patch_manager.get_device_name_suggestions(chip_name)
                error_msg = f"设备 '{chip_name}' 不受支持。\n{suggestions}"

        return {
            "success": False,
            "serial_number": None,
            "message": error_msg
        }


def disconnect_device() -> Dict[str, Any]:
    """断开 JLink 设备连接.

    断开当前活动的 JLink 连接，释放设备资源。
    建议在不使用时调用此函数。

    Returns:
        断开结果，包含:
        - success: 是否成功
        - message: 状态信息
    """
    try:
        jlink_manager.disconnect()
        logger.info("设备已断开连接")
        return {
            "success": True,
            "message": "设备已断开连接"
        }
    except Exception as e:
        logger.error(f"断开连接失败: {e}")
        return {
            "success": False,
            "message": str(e)
        }


def get_connection_status() -> Dict[str, Any]:
    """获取当前连接状态.

    查询 JLink 连接状态、目标芯片连接状态、电压等信息。

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - data: 连接状态数据
            - connected: 是否已连接
            - device_serial: 设备序列号
            - target_interface: 目标接口类型
            - target_voltage: 目标电压（V）
            - target_connected: 目标芯片是否已连接
            - firmware_version: JLink 固件版本
        - message: 状态信息
    """
    try:
        status = jlink_manager.get_connection_status()
        return {
            "success": True,
            "data": status.model_dump(),
            "message": "获取连接状态成功"
        }
    except Exception as e:
        logger.error(f"获取连接状态失败: {e}")
        from ..exceptions import JLinkErrorCode
        return {
            "success": False,
            "data": None,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请检查 JLink 设备连接状态"
            }
        }
