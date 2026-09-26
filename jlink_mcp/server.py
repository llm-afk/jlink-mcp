"""JLink MCP 服务器 - 核心服务实现.

这是一个 MCP (Model Context Protocol) 服务器，提供与 JLink 调试器交互的工具接口。
支持的功能包括：
- 连接管理：枚举设备、连接/断开、状态查询
- 设备信息：读取芯片信息、电压
- 内存操作：读写内存、寄存器
- Flash 操作：擦除、烧录、校验
- 调试控制：复位、运行/暂停、单步、断点
- RTT：实时传输日志
- GDB Server：启动/停止 GDB 调试服务器
"""

from contextlib import asynccontextmanager

import anyio
from mcp.server.fastmcp import FastMCP
from pydantic import StrictBool, StrictInt

from .jlink_manager import jlink_manager
from .executor import SerialToolExecutor
from .models.parameters import Address
from .utils import logger
from .config_manager import config_manager

# 导入所有工具函数（使用别名避免命名冲突）
from .tools.connection import (
    list_jlink_devices as _list_jlink_devices,
    connect_device as _connect_device,
    disconnect_device as _disconnect_device,
    get_connection_status as _get_connection_status,
    match_chip_name as _match_chip_name,
)
from .tools.device_info import (
    get_target_info as _get_target_info,
    get_target_voltage as _get_target_voltage,
    scan_target_devices as _scan_target_devices,
    list_device_patches as _list_device_patches,
)
from .tools.memory import (
    read_memory as _read_memory,
    write_memory as _write_memory,
    read_registers as _read_registers,
    write_register as _write_register,
)
from .tools.flash import (
    erase_flash as _erase_flash,
    erase_sector as _erase_sector,
    program_flash as _program_flash,
    verify_flash as _verify_flash,
)
from .tools.debug import (
    reset_target as _reset_target,
    halt_cpu as _halt_cpu,
    run_cpu as _run_cpu,
    step_instruction as _step_instruction,
    get_cpu_state as _get_cpu_state,
    set_breakpoint as _set_breakpoint,
    clear_breakpoint as _clear_breakpoint,
)
from .tools.rtt import (
    rtt_start as _rtt_start,
    rtt_stop as _rtt_stop,
    rtt_read as _rtt_read,
    rtt_write as _rtt_write,
    rtt_get_status as _rtt_get_status,
)
from .gdb_server import (
    start_gdb_server as _start_gdb_server,
    stop_gdb_server as _stop_gdb_server,
    get_gdb_server_status as _get_gdb_server_status,
)
from .tools.svd import (
    list_svd_devices as _list_svd_devices,
    get_svd_peripherals as _get_svd_peripherals,
    get_svd_registers as _get_svd_registers,
    read_register_with_fields as _read_register_with_fields,
    parse_register_value as _parse_register_value,
    read_register_by_address as _read_register_by_address,
    write_register_by_address as _write_register_by_address,
)
from .tools.guidance import (
    get_usage_guidance as _get_usage_guidance,
    get_best_practices as _get_best_practices,
    list_scenarios as _list_scenarios,
    get_forbidden_operations as _get_forbidden_operations,
)

# Native J-Link handles stay on the same worker for their entire lifetime.
hardware_executor = SerialToolExecutor("jlink-hardware")
metadata_executor = SerialToolExecutor("jlink-svd")


def _cleanup_hardware():
    from .gdb_server import gdb_server_manager

    try:
        gdb_server_manager.stop()
    finally:
        jlink_manager.disconnect()


@asynccontextmanager
async def server_lifespan(server):
    try:
        yield {}
    finally:
        # A cancelled request must not interrupt closing the probe or GDB process.
        with anyio.CancelScope(shield=True):
            try:
                await hardware_executor.aclose(_cleanup_hardware)
            finally:
                await metadata_executor.aclose()


mcp = FastMCP("jlink-mcp-server", lifespan=server_lifespan)


# ========================================
# 连接管理工具 (4个)
# ========================================

@mcp.tool()
async def list_jlink_devices() -> list[dict]:
    """列出所有连接的 JLink 设备.

    返回系统中所有已连接的 JLink 调试器列表。
    每个设备包含序列号、产品名称和连接类型。

    Returns:
        设备信息列表
    """
    return await hardware_executor.run(_list_jlink_devices)


@mcp.tool()
async def connect_device(serial_number: str | None = None, interface: str = "SWD", chip_name: str | None = None, jlink_path: str | None = None) -> dict:
    """连接到 JLink 设备.

    连接到指定的 JLink 调试器。如果不指定序列号，则连接第一个可用设备。

    Args:
        serial_number: 设备序列号（可选）
        interface: 目标接口类型（SWD/JTAG，默认 SWD）
        chip_name: 目标芯片名称（如 STM32F407VG，可选）
        jlink_path: 指定 JLink 安装目录或 JLink_x64.dll 文件路径（可选，
            如 D:\\Program Files\\SEGGER\\JLink_V942）；None 则自动选择最新版本

    Returns:
        连接结果
    """
    return await hardware_executor.run(_connect_device, serial_number, interface, chip_name, jlink_path)


@mcp.tool()
async def disconnect_device() -> dict:
    """断开 JLink 设备连接.

    断开当前活动的 JLink 连接，释放设备资源。

    Returns:
        断开结果
    """
    return await hardware_executor.run(_disconnect_device)


@mcp.tool()
async def get_connection_status() -> dict:
    """获取当前连接状态.

    查询 JLink 连接状态、目标芯片连接状态、电压等信息。

    Returns:
        连接状态信息
    """
    return await hardware_executor.run(_get_connection_status)


@mcp.tool()
async def match_chip_name(chip_name: str) -> dict:
    """智能匹配芯片名称.

    将简化的芯片名称（如 FC7300F4MDD）匹配到完整的芯片名称
    （如 FC7300F4MDDxXxxxT1C）。支持前缀匹配、包含匹配和模糊匹配。

    Args:
        chip_name: 芯片名称（可以是简化名称或完整名称）

    Returns:
        匹配结果，包含 matched（匹配到的完整名称）和 all_matches（所有匹配项）
    """
    return await hardware_executor.run(_match_chip_name, chip_name)


# ========================================
# 设备信息工具 (3个)
# ========================================

@mcp.tool()
async def get_target_info() -> dict:
    """获取目标设备（MCU）信息.

    读取连接的目标芯片的详细信息，包括设备名称、内核类型、Flash/RAM 大小等。

    Returns:
        目标设备信息
    """
    return await hardware_executor.run(_get_target_info)


@mcp.tool()
async def get_target_voltage() -> dict:
    """获取目标电压.

    读取目标芯片的供电电压。

    Returns:
        电压信息
    """
    return await hardware_executor.run(_get_target_voltage)


@mcp.tool()
async def scan_target_devices() -> dict:
    """扫描目标总线上的设备.

    扫描 JTAG 链或 SWD 总线上的所有设备。

    Returns:
        扫描结果
    """
    return await hardware_executor.run(_scan_target_devices)


@mcp.tool()
async def list_device_patches() -> dict:
    """列出所有已加载的设备补丁及其支持的设备.

    列出所有可用的设备补丁（如 Flagchip 等）及其支持的设备列表。

    Returns:
        设备补丁列表
    """
    return await hardware_executor.run(_list_device_patches)


# ========================================
# 内存操作工具 (4个)
# ========================================

@mcp.tool()
async def read_memory(address: Address, size: StrictInt, width: StrictInt = 32) -> dict:
    """读取指定地址的内存.

    Args:
        address: 起始地址
        size: 读取大小（字节，最大 64KB）
        width: 访问宽度（8/16/32位，默认 32）

    Returns:
        内存读取结果
    """
    return await hardware_executor.run(_read_memory, address, size, width)


@mcp.tool()
async def write_memory(address: Address, data: str, width: StrictInt = 32) -> dict:
    """写入内存.

    Args:
        address: 起始地址
        data: 要写入的数据
        width: 访问宽度（8/16/32位，默认 32）

    Returns:
        写入结果
    """
    return await hardware_executor.run(_write_memory, address, data, width)


@mcp.tool()
async def read_registers(register_names: list[str] | None = None) -> dict:
    """读取 CPU 寄存器.

    Args:
        register_names: 寄存器名称列表（可选，None 则读取所有通用寄存器）

    Returns:
        寄存器值
    """
    return await hardware_executor.run(_read_registers, register_names)


@mcp.tool()
async def write_register(register_name: str, value: StrictInt) -> dict:
    """写入单个寄存器.

    Args:
        register_name: 寄存器名称
        value: 寄存器值

    Returns:
        写入结果
    """
    return await hardware_executor.run(_write_register, register_name, value)


# ========================================
# Flash 操作工具 (4个)
# ========================================

@mcp.tool()
async def erase_flash(
    start_address: Address | None = None,
    end_address: Address | None = None,
    chip_erase: StrictBool = False
) -> dict:
    """显式执行整片 Flash 擦除；范围擦除请使用 erase_sector.

    Args:
        start_address: 兼容旧接口，传入将报错，不会擦除
        end_address: 兼容旧接口，传入将报错，不会擦除
        chip_erase: 必须为 True；会清空整个 Flash（含 bootloader）

    Returns:
        擦除结果
    """
    return await hardware_executor.run(_erase_flash, start_address, end_address, chip_erase)


@mcp.tool()
async def erase_sector(address: Address, count: StrictInt = 1, page_size: StrictInt = 1024) -> dict:
    """按扇区（页）擦除 Flash.

    Args:
        address: 要擦除页内的任意地址（自动对齐到页边界）
        count: 连续擦除的页数（默认 1）
        page_size: 页大小（字节，默认 1024，GD32C10x 为 1KB）

    Returns:
        擦除结果
    """
    return await hardware_executor.run(_erase_sector, address, count, page_size)


@mcp.tool()
async def program_flash(address: Address, data: str | None = None, verify: StrictBool = True, file_path: str | None = None) -> dict:
    """烧录固件到 Flash.

    Args:
        address: 起始地址
        data: 要烧录的数据
        verify: 烧录后是否校验
        file_path: 固件文件路径（与 data 二选一）

    Returns:
        烧录结果
    """
    return await hardware_executor.run(_program_flash, address, data, verify, file_path)


@mcp.tool()
async def verify_flash(address: Address, data: str) -> dict:
    """校验 Flash 内容.

    Args:
        address: 起始地址
        data: 期望的数据

    Returns:
        校验结果
    """
    return await hardware_executor.run(_verify_flash, address, data)


# ========================================
# 调试控制工具 (7个)
# ========================================

@mcp.tool()
async def reset_target(reset_type: str = "normal") -> dict:
    """复位目标芯片.

    Args:
        reset_type: 复位类型（normal/halt/core）

    Returns:
        复位结果
    """
    return await hardware_executor.run(_reset_target, reset_type)


@mcp.tool()
async def halt_cpu() -> dict:
    """暂停 CPU.

    Returns:
        暂停结果
    """
    return await hardware_executor.run(_halt_cpu)


@mcp.tool()
async def run_cpu() -> dict:
    """运行 CPU.

    Returns:
        运行结果
    """
    return await hardware_executor.run(_run_cpu)


@mcp.tool()
async def step_instruction() -> dict:
    """单步执行一条指令.

    Returns:
        单步执行结果
    """
    return await hardware_executor.run(_step_instruction)


@mcp.tool()
async def get_cpu_state() -> dict:
    """获取 CPU 状态.

    Returns:
        CPU 状态信息
    """
    return await hardware_executor.run(_get_cpu_state)


@mcp.tool()
async def set_breakpoint(address: Address) -> dict:
    """设置断点.

    Args:
        address: 断点地址

    Returns:
        设置结果
    """
    return await hardware_executor.run(_set_breakpoint, address)


@mcp.tool()
async def clear_breakpoint(address: Address) -> dict:
    """清除断点.

    Args:
        address: 断点地址

    Returns:
        清除结果
    """
    return await hardware_executor.run(_clear_breakpoint, address)


# ========================================
# RTT 工具 (5个)
# ========================================

@mcp.tool()
async def rtt_start(
    buffer_index: StrictInt = 0,
    read_mode: str = "continuous",
    timeout_ms: StrictInt = 1000,
    block_address: Address | None = None
) -> dict:
    """启动 RTT.

    Args:
        buffer_index: RTT 缓冲区索引
        read_mode: 读取模式
        timeout_ms: 超时时间（毫秒）
        block_address: 可选 RTT 控制块地址，None 时由 J-Link 搜索

    Returns:
        启动结果
    """
    return await hardware_executor.run(_rtt_start, buffer_index, read_mode, timeout_ms, block_address)


@mcp.tool()
async def rtt_stop() -> dict:
    """停止 RTT.

    Returns:
        停止结果
    """
    return await hardware_executor.run(_rtt_stop)


@mcp.tool()
async def rtt_read(
    buffer_index: StrictInt | None = None,
    size: StrictInt = 1024,
    timeout_ms: StrictInt | None = None
) -> dict:
    """读取 RTT 日志.

    Args:
        buffer_index: RTT 缓冲区索引，None 使用启动时的配置
        size: 读取大小（字节）
        timeout_ms: 超时时间（毫秒）

    Returns:
        读取结果
    """
    return await hardware_executor.run(_rtt_read, buffer_index, size, timeout_ms)


@mcp.tool()
async def rtt_write(data: str, buffer_index: StrictInt | None = None) -> dict:
    """向 RTT 写入数据.

    Args:
        data: 要写入的数据
        buffer_index: RTT 缓冲区索引，None 使用启动时的配置

    Returns:
        写入结果
    """
    return await hardware_executor.run(_rtt_write, data, buffer_index)


@mcp.tool()
async def rtt_get_status() -> dict:
    """获取 RTT 状态.

    Returns:
        RTT 状态信息
    """
    return await hardware_executor.run(_rtt_get_status)


# ========================================
# GDB Server 工具 (3个)
# ========================================

@mcp.tool()
async def start_gdb_server(
    host: str = "127.0.0.1",
    port: StrictInt = 2331,
    device: str | None = None,
    interface: str | None = None,
    speed: StrictInt = 4000,
    jlink_path: str | None = None,
    transfer_connection: StrictBool = False
) -> dict:
    """启动 GDB Server.

    Args:
        host: 127.0.0.1 仅本机，0.0.0.0 允许远程连接
        port: 监听端口
        device: 设备名称
        interface: 接口类型（SWD/JTAG，None 继承当前连接或使用 SWD）
        speed: 接口速度（kHz）
        jlink_path: 指定 JLink 安装目录或 GDB Server 可执行文件路径（可选，
            如 D:\\Program Files\\SEGGER\\JLink_V942）；None 则自动选择最新版本
        transfer_connection: 显式允许释放 MCP 当前连接，将探针交给 GDB；停止后需重新连接

    Returns:
        启动结果
    """
    return await hardware_executor.run(_start_gdb_server, host, port, device, interface, speed, jlink_path, transfer_connection)


@mcp.tool()
async def stop_gdb_server() -> dict:
    """停止 GDB Server.

    Returns:
        停止结果
    """
    return await hardware_executor.run(_stop_gdb_server)


@mcp.tool()
async def get_gdb_server_status() -> dict:
    """获取 GDB Server 状态.

    Returns:
        GDB Server 状态信息
    """
    return await hardware_executor.run(_get_gdb_server_status)


# ========================================
# SVD 工具 (5个)
# ========================================

@mcp.tool()
async def list_svd_devices() -> dict:
    """列出所有支持 SVD 的设备.

    返回系统中所有已加载 SVD 文件的设备列表。

    Returns:
        设备列表
    """
    return await metadata_executor.run(_list_svd_devices)


@mcp.tool()
async def get_svd_peripherals(device_name: str) -> dict:
    """获取指定设备的所有外设.

    Args:
        device_name: 设备名称（如 FC4150F1MBSxXxxxT1A）

    Returns:
        外设列表
    """
    return await metadata_executor.run(_get_svd_peripherals, device_name)


@mcp.tool()
async def get_svd_registers(device_name: str, peripheral_name: str) -> dict:
    """获取指定外设的所有寄存器.

    Args:
        device_name: 设备名称
        peripheral_name: 外设名称

    Returns:
        寄存器列表
    """
    return await metadata_executor.run(_get_svd_registers, device_name, peripheral_name)


@mcp.tool()
async def read_register_with_fields(
    device_name: str,
    peripheral_name: str,
    register_name: str
) -> dict:
    """读取寄存器并解析字段（结合 SVD 和实际读取）.

    Args:
        device_name: 设备名称
        peripheral_name: 外设名称
        register_name: 寄存器名称

    Returns:
        寄存器值和字段解析结果
    """
    return await hardware_executor.run(_read_register_with_fields, device_name, peripheral_name, register_name)


@mcp.tool()
async def parse_register_value(
    device_name: str,
    peripheral_name: str,
    register_name: str,
    value: StrictInt
) -> dict:
    """解析寄存器值（仅解析，不读取硬件）.

    Args:
        device_name: 设备名称
        peripheral_name: 外设名称
        register_name: 寄存器名称
        value: 寄存器值

    Returns:
        寄存器值和字段解析结果
    """
    return await metadata_executor.run(_parse_register_value, device_name, peripheral_name, register_name, value)


@mcp.tool()
async def read_register_by_address(
    address: Address,
    width: StrictInt = 32,
    device_name: str | None = None
) -> dict:
    """按地址直接读取寄存器值（不依赖 SVD）.

    无需 SVD 文件即可按绝对地址读取寄存器值。
    如果同时提供 device_name 且 SVD 可用，会尝试反查该地址对应的
    外设/寄存器名称并解析字段。

    Args:
        address: 寄存器绝对地址
        width: 访问宽度（8/16/32位，默认 32）
        device_name: 可选，用于反查寄存器名和字段解析

    Returns:
        寄存器值和可选的字段解析结果
    """
    return await hardware_executor.run(_read_register_by_address, address, width, device_name)


@mcp.tool()
async def write_register_by_address(
    address: Address,
    value: StrictInt,
    width: StrictInt = 32
) -> dict:
    """按地址直接写入寄存器值（不依赖 SVD）.

    无需 SVD 文件即可按绝对地址写入寄存器值。

    Args:
        address: 寄存器绝对地址
        value: 要写入的值
        width: 访问宽度（8/16/32位，默认 32）

    Returns:
        写入结果
    """
    return await hardware_executor.run(_write_register_by_address, address, value, width)


# ========================================
# 使用指南和配置管理工具 (5个)
# ========================================

@mcp.tool()
async def get_usage_guidance(category: str | None = None, include_examples: StrictBool = True) -> dict:
    """获取 JLink MCP 工具使用指南.

    提供所有可用工具的分类、描述和使用示例。

    Args:
        category: 工具分类（可选），支持：
            - connection: 连接管理
            - device_info: 设备信息
            - memory: 内存操作
            - flash: Flash 操作
            - debug: 调试控制
            - rtt: RTT 日志
            - svd: SVD 寄存器解析
            None 表示返回所有分类
        include_examples: 是否包含使用示例

    Returns:
        使用指南，包含工具分类、描述和常见场景
    """
    return _get_usage_guidance(category, include_examples)


@mcp.tool()
async def get_best_practices(task_type: str) -> dict:
    """获取指定任务类型的最佳实践.

    Args:
        task_type: 任务类型，支持：
            - read_registers: 读取寄存器
            - connect_device: 连接设备
            - memory_operations: 内存操作
            - flash_operations: Flash 操作
            - debug: 调试控制

    Returns:
        最佳实践，包含推荐流程、禁止操作和常见错误
    """
    return _get_best_practices(task_type)


@mcp.tool()
async def list_scenarios() -> dict:
    """列出所有可用的使用场景.

    Returns:
        场景列表，包含描述和预期时间
    """
    return _list_scenarios()


@mcp.tool()
async def get_forbidden_operations() -> dict:
    """获取禁止的操作列表.

    Returns:
        禁止操作列表和原因说明
    """
    return _get_forbidden_operations()


@mcp.tool()
async def get_system_prompt(prompt_name: str | None = None) -> dict:
    """获取系统提示词或自定义提示词.

    Args:
        prompt_name: 提示词名称（可选），None 则返回系统提示词

    Returns:
        提示词内容
    """
    try:
        if prompt_name:
            prompt = config_manager.get_custom_prompt(prompt_name)
            if prompt is None:
                available = list(config_manager.list_custom_prompts().keys())
                return {
                    "success": False,
                    "error": f"提示词 '{prompt_name}' 不存在",
                    "available_prompts": available
                }
            return {
                "success": True,
                "prompt_name": prompt_name,
                "prompt": prompt
            }
        else:
            prompt = config_manager.get_system_prompt()
            return {
                "success": True,
                "prompt_name": "system",
                "prompt": prompt
            }
    except Exception as e:
        logger.error(f"获取提示词失败: {e}")
        return {
            "success": False,
            "error": str(e)
        }


def init_server_config():
    """初始化服务器配置.

    设置系统提示词和自定义提示词。
    """
    # 添加常见的自定义提示词
    config_manager.add_custom_prompt(
        "device_debug",
        """设备调试通用指南：
1. 使用正确的接口类型（JTAG/SWD）
2. 可以使用芯片名称缩写，系统尝试自动匹配
3. 读取寄存器/内存前必须暂停 CPU（halt_cpu）
4. 连接失败时使用 match_chip_name() 验证设备名称
5. 使用 list_device_patches() 查看支持的设备补丁"""
    )

    config_manager.add_custom_prompt(
        "memory_debug",
        """内存调试指南：
1. 读写内存前确保 CPU 已暂停（halt_cpu()）
2. 验证地址范围是否有效
3. 选择合适的访问宽度（8/16/32 位）
4. 批量读写比多次单次读写更高效
5. 最大读取大小限制为 64KB"""
    )

    config_manager.add_custom_prompt(
        "flash_programming",
        """Flash 烧录指南：
1. 使用目标支持的 SWD/JTAG 接口连接设备
2. 按需擦除；整片擦除显式使用 erase_flash(chip_erase=True)，会清空 bootloader
3. 烧录时启用校验（verify=True）
4. Flash 操作较慢，需要耐心等待
5. 烧录完成后复位设备（reset_target()）"""
    )

    logger.info("服务器配置初始化完成")


def main():
    """MCP 服务器入口函数."""
    logger.info("启动 JLink MCP 服务器 (std.io 模式)")

    # 初始化服务器配置
    init_server_config()

    # 启动 MCP 服务器
    mcp.run(transport='stdio')


if __name__ == "__main__":
    main()
