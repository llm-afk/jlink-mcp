"""使用指南和帮助工具函数.

提供 MCP 工具的使用指南、示例和最佳实践。
此模块不依赖硬件连接，可直接调用。
"""

from typing import Dict, Any, List, Optional

from ..utils import logger


# 工具分类定义
TOOL_CATEGORIES = {
    "连接管理": {
        "description": "JLink 设备连接和状态管理",
        "tools": [
            "list_jlink_devices",
            "connect_device",
            "disconnect_device",
            "get_connection_status",
            "match_chip_name"
        ]
    },
    "设备信息": {
        "description": "读取目标芯片信息",
        "tools": [
            "get_target_info",
            "get_target_voltage",
            "scan_target_devices",
            "list_device_patches"
        ]
    },
    "内存操作": {
        "description": "读写内存和寄存器",
        "tools": [
            "read_memory",
            "write_memory",
            "read_registers",
            "write_register"
        ]
    },
    "Flash 操作": {
        "description": "Flash 擦除、烧录和校验",
        "tools": [
            "erase_flash",
            "erase_sector",
            "program_flash",
            "verify_flash"
        ]
    },
    "调试控制": {
        "description": "CPU 控制和断点管理",
        "tools": [
            "reset_target",
            "halt_cpu",
            "run_cpu",
            "step_instruction",
            "get_cpu_state",
            "set_breakpoint",
            "clear_breakpoint"
        ]
    },
    "RTT": {
        "description": "实时传输（Real Time Transfer）日志",
        "tools": [
            "rtt_start",
            "rtt_stop",
            "rtt_read",
            "rtt_write",
            "rtt_get_status"
        ]
    },
    "SVD": {
        "description": "SVD 寄存器解析",
        "tools": [
            "list_svd_devices",
            "get_svd_peripherals",
            "get_svd_registers",
            "read_register_with_fields",
            "parse_register_value",
            "read_register_by_address",
            "write_register_by_address"
        ]
    },
    "GDB Server": {
        "description": "GDB 调试服务器",
        "tools": [
            "start_gdb_server",
            "stop_gdb_server",
            "get_gdb_server_status"
        ]
    },
    "辅助信息": {
        "description": "使用指南、最佳实践与系统提示词",
        "tools": [
            "get_usage_guidance",
            "get_best_practices",
            "list_scenarios",
            "get_forbidden_operations",
            "get_system_prompt"
        ]
    }
}


# 常见使用场景和工作流程
USAGE_SCENARIOS = {
    "首次连接": {
        "description": "首次连接 JLink 设备并获取基本信息",
        "steps": [
            "1. list_jlink_devices() - 列出可用设备",
            "2. connect_device(chip_name='GD32C103VB', interface='SWD') - 连接设备",
            "3. get_connection_status() - 确认连接状态",
            "4. get_target_info() - 获取芯片信息",
            "5. get_target_voltage() - 检查供电电压"
        ],
        "example": "connect_device(chip_name='GD32C103VB', interface='SWD')",
        "expected_time": "10-30 秒"
    },
    "读取寄存器": {
        "description": "读取指定寄存器的值并解析字段",
        "steps": [
            "1. connect_device(chip_name='GD32C103VB', interface='SWD') - 连接设备",
            "2. 若需要一致快照，先 halt_cpu；普通外设读取可以保持运行",
            "3. read_register_with_fields(device_name, peripheral_name, register_name) - 读取寄存器"
        ],
        "example": "read_register_with_fields('GD32C10x', 'GPIOA', 'CTL0')",
        "note": "CPU 寄存器需要暂停；SVD 外设读取不主动暂停目标",
        "forbidden": [
            "不要调用 get_svd_peripherals() 遍历外设",
            "不要调用 get_svd_registers() 获取寄存器列表（除非未知地址）",
            "可结合用户源码核对地址和用途",
            "不要对读清零/FIFO 等寄存器盲目重复读取"
        ],
        "expected_time": "2-5 秒"
    },
    "写入内存": {
        "description": "向指定内存地址写入数据",
        "steps": [
            "1. connect_device(serial_number, interface) - 连接设备",
            "2. halt_cpu() - 暂停 CPU（如果目标正在运行）",
            "3. write_memory(address, data, width) - 写入数据",
            "4. run_cpu() - 恢复运行（可选）"
        ],
        "example": "write_memory(address=0x20000000, data='00 01 02 03', width=32)",
        "expected_time": "2-5 秒"
    },
    "Flash 烧录": {
        "description": "擦除 Flash 并烧录固件",
        "steps": [
            "1. connect_device(chip_name, interface='SWD') - 连接设备",
            "2. get_connection_status() - 确认连接",
            "3. program_flash 会处理目标扇区；仅需清空页面时使用 erase_sector(address, count, page_size)",
            "4. program_flash(address, data, verify=True) - 烧录并校验",
            "5. verify_flash(address, data) - 再次校验（可选）"
        ],
        "note": "Flash 操作较慢，需要耐心等待",
        "expected_time": "30-120 秒"
    },
    "设置断点调试": {
        "description": "设置断点并单步调试",
        "steps": [
            "1. connect_device(chip_name, interface) - 连接设备",
            "2. reset_target(reset_type='halt') - 复位并暂停",
            "3. set_breakpoint(address) - 设置断点",
            "4. run_cpu() - 运行到断点",
            "5. get_cpu_state() - 查看 CPU 状态",
            "6. read_registers() - 读取寄存器",
            "7. step_instruction() - 单步执行",
            "8. clear_breakpoint(address) - 清除断点（调试完成后）"
        ],
        "expected_time": "取决于调试复杂度"
    },
    "RTT 日志调试": {
        "description": "使用 RTT 查看实时日志",
        "steps": [
            "1. connect_device(serial_number, interface) - 连接设备",
            "2. rtt_start(buffer_index=0) - 启动 RTT",
            "3. rtt_read(size=1024) - 读取日志",
            "4. rtt_write(data) - 发送命令（可选）",
            "5. rtt_stop() - 停止 RTT（完成后）"
        ],
        "expected_time": "持续使用"
    },
    "计算波特率": {
        "description": "计算 CAN 控制器的波特率配置",
        "steps": [
            "1. connect_device(chip_name='FC7300F4MDD', interface='JTAG') - 连接设备",
            "2. halt_cpu() - 暂停 CPU（必需）",
            "3. read_register_with_fields(device, 'FLEXCAN0', 'MCR') - 检查 CAN FD 是否启用（FDEN 位）",
            "4. read_register_with_fields(device, 'FLEXCAN0', 'CTRL1') - 读取时钟源配置（CLKSRC 位）",
            "5. read_register_with_fields(device, 'FLEXCAN0', 'CTRL2') - 检查位时序扩展（BTE 位）",
            "6. 如果 BTE=1：读取 EPRS、ENCBT、EDCBT 寄存器",
            "7. 如果 BTE=0：读取 CTRL1、CBT 寄存器",
            "8. 计算：使用 PE 时钟（默认 24MHz）计算标称和数据相位波特率"
        ],
        "example": "PE=24MHz, ENPRESDIV=3, NTSEG1=7, NTSEG2=2 → 标称 500kbps, 数据 2Mbps (CAN FD)",
        "note": "默认使用 24MHz PE 时钟。如需精确计算，请通过 SCG 寄存器确认实际时钟频率",
        "expected_time": "5-10 秒"
    }
}


# 最佳实践定义
BEST_PRACTICES = {
    "read_registers": {
        "title": "读取寄存器最佳实践",
        "recommended_flow": [
            "1. connect_device(chip_name='GD32C103VB', interface='SWD') - 连接设备",
            "2. 需要一致快照时 halt_cpu；实时外设读取可保持运行",
            "3. read_register_with_fields(device_name, peripheral_name, register_name) - 直接读取寄存器"
        ],
        "forbidden": [
            "优先结合 SVD 与用户源码核对寄存器地址和副作用",
            "禁止调用 get_svd_peripherals() 遍历所有外设",
            "禁止重复调用 get_svd_registers() 获取相同的寄存器列表",
            "CPU 寄存器读取必须确认暂停；普通外设读取不主动暂停"
        ],
        "performance_tips": [
            "同一探针操作串行执行；需要多个 CPU 寄存器时用 read_registers 批量读取",
            "缓存外设和寄存器信息，避免重复查询",
            "最小化数据传输，只读取必要的字段",
            "使用芯片名称（如 GD32C103VB、STM32F407VG）"
        ],
        "common_mistakes": [
            "❌ 混淆 CPU 寄存器与内存映射外设寄存器",
            "✅ CPU 寄存器须稳定暂停，实时外设观察保持运行",
            "❌ 混淆 SVD 设备名与芯片名（SVD 用 'GD32C10x'，芯片名用 'GD32C103VB'）",
            "✅ 用 list_svd_devices() 查看可用 SVD 设备名",
            "❌ 调用 get_svd_peripherals() → get_svd_registers() → read_register_with_fields()（太慢）",
            "✅ 直接调用 read_register_with_fields()（快速）",
            "❌ 连接失败后重复尝试相同的连接方式",
            "✅ 分析错误信息，使用 match_chip_name() 验证名称"
        ]
    },
    "connect_device": {
        "title": "连接设备最佳实践",
        "recommended_flow": [
            "1. list_jlink_devices() - 列出可用设备",
            "2. connect_device(chip_name='GD32C103VB', interface='SWD') - 连接设备",
            "3. get_connection_status() - 确认连接状态",
            "4. 连接失败时使用 match_chip_name() 验证名称"
        ],
        "recommended_practices": [
            "使用芯片名称（如 GD32C103VB、STM32F407VG）",
            "接口默认使用 SWD（全局默认，接线少、速度快），除非目标仅支持 JTAG",
            "连接失败时使用 match_chip_name() 验证名称",
            "优先使用芯片名称而非序列号（更灵活）"
        ],
        "interface_selection": [
            "SWD：默认推荐（Cortex-M 系列通用，2 线接线少、速度快）",
            "JTAG：仅当目标只支持 JTAG 或明确需要时使用"
        ],
        "forbidden": [
            "禁止重复尝试相同的连接参数",
            "禁止在连接失败后立即再次尝试（先分析错误）",
            "禁止盲目切换接口类型（先了解目标芯片要求）"
        ],
        "troubleshooting": {
            "Unsupported device": "使用 list_device_patches() 查看支持的设备，或使用 match_chip_name() 验证名称",
            "No target connected": "检查目标芯片供电（3.3V），确保 SWD 连接正确",
            "Unknown DEV_ID": "尝试不同的接口类型（SWD/JTAG），或检查硬件连接"
        }
    },
    "memory_operations": {
        "title": "内存操作最佳实践",
        "recommended_flow": [
            "内存读写保持 CPU 原状态；需要一致快照时先显式暂停",
            "验证地址范围是否有效",
            "选择合适的访问宽度（8/16/32 位）",
            "仅在本次主动暂停且需要恢复时调用 run_cpu()"
        ],
        "forbidden": [
            "不要把运行中变化的数据当作一致快照",
            "禁止写入只读内存区域"
        ],
        "performance_tips": [
            "批量读写比多次单次读写更高效",
            "使用 hex_dump 参数查看数据格式",
            "注意最大读取大小限制（64KB）"
        ]
    },
    "calculate_baudrate": {
        "title": "计算 CAN 波特率最佳实践",
        "recommended_flow": [
            "1. connect_device(chip_name='FC7300F4MDD', interface='JTAG') - 连接设备",
            "2. halt_cpu() - 暂停 CPU（必需）",
            "3. 读取 MCR 寄存器 - 检查 FDEN 位（CAN FD 启用状态）",
            "4. 读取 CTRL2 寄存器 - 检查 BTE 位（位时序扩展启用状态）",
            "5. 根据配置读取时序寄存器",
            "   - BTE=0: 读取 CTRL1（PRESDIV, PROPSEG, PSEG1, PSEG2）和 CBT",
            "   - BTE=1: 读取 EPRS（ENPRESDIV, EDPRESDIV）和 ENCBT、EDCBT",
            "6. 使用公式计算标称相位和数据相位波特率"
        ],
        "calculation_formula": [
            "时间量子 TQ = 1 / (PE_Clock / (PRESDIV + 1))",
            "位时间 = (1 + TSEG1 + TSEG2) × TQ",
            "波特率 = 1 / 位时间 = PE_Clock / ((PRESDIV + 1) × (1 + TSEG1 + TSEG2))"
        ],
        "default_values": {
            "PE_Clock": "24MHz（默认值，除非用户特别说明）",
            "注意": "实际波特率取决于系统时钟配置，建议查看 SCG 寄存器确认"
        },
        "forbidden": [
            "禁止在 CPU 运行时读取寄存器",
            "禁止忽略 CAN FD 配置（会影响数据相位波特率）",
            "禁止混淆标称相位和数据相位的预分频器",
            "禁止忽略 TSEG 的 +1 偏移（实际时间 = TSEG + 1）"
        ],
        "common_mistakes": [
            "❌ 使用错误预分频器（标称 vs 数据）",
            "✅ 标称相位用 ENPRESDIV，数据相位用 EDPRESDIV",
            "❌ 忽略 TSEG 的 +1 偏移",
            "✅ 时间段 = TSEG + 1（例如：NTSEG1=7 表示 8 个时间量子）",
            "❌ 假设 PE 时钟而不验证",
            "✅ 默认 24MHz，但建议从时钟树配置确认",
            "❌ 忽略 BTE 位，使用错误的寄存器",
            "✅ BTE=0 用 CTRL1/CBT，BTE=1 用 EPRS/ENCBT/EDCBT"
        ]
    },
    "flash_operations": {
        "title": "Flash 操作最佳实践",
        "recommended_flow": [
            "1. connect_device(chip_name, interface) - 连接设备",
            "2. halt_cpu() - 暂停 CPU（Flash 操作前建议暂停）",
            "3. 通常无需预擦除；仅明确清空全片时 erase_flash(chip_erase=True)",
            "4. program_flash(address, data, verify=True) - 烧录固件",
            "5. verify_flash(address, data) - 校验烧录结果（可选）"
        ],
        "forbidden": [
            "不要用 write_memory 代替 program_flash 烧录 Flash",
            "禁止烧录到非 Flash 地址（如 RAM/外设区）",
            "禁止在烧录失败后直接运行 CPU（先确认烧录完整性）"
        ],
        "performance_tips": [
            "使用 file_path 从原始 .bin 烧录；HEX/ELF 须先转换并核对装载地址",
            "烧录大固件时关闭 verify 可提速（烧录后再单独校验一次）",
            "避免无必要的整片擦除，以保留其他镜像和参数"
        ],
        "common_mistakes": [
            "❌ 把范围擦除误当整片擦除或将 HEX/ELF 当原始 BIN",
            "✅ 通常直接 program_flash(..., verify=True)，检查 success 和 verify_result",
            "❌ 烧录后未校验就复位运行",
            "✅ 使用 verify=True 或单独调用 verify_flash"
        ]
    },
    "debug": {
        "title": "调试控制最佳实践",
        "recommended_flow": [
            "1. connect_device(chip_name, interface) - 连接设备",
            "2. reset_target(reset_type='halt') - 复位并暂停",
            "3. set_breakpoint(address) - 在关键位置设置断点",
            "4. run_cpu() - 运行到断点",
            "5. read_registers() / get_cpu_state() - 查看状态",
            "6. step_instruction() - 单步执行",
            "7. clear_breakpoint(address) - 调试完成后清除断点"
        ],
        "forbidden": [
            "禁止在 CPU 运行时设置断点（先 halt_cpu）",
            "禁止忘记清除断点（可能影响后续运行）",
            "禁止在复位后不确认状态就继续操作"
        ],
        "performance_tips": [
            "使用 halt_cpu 后再批量读取寄存器，避免重复暂停",
            "断点命中后先看 PC 和调用栈，再决定单步方向"
        ],
        "common_mistakes": [
            "❌ 运行态直接读寄存器（值不准确）",
            "✅ 先 halt_cpu 再读",
            "❌ 单步前不确认已在暂停态",
            "✅ 先 get_cpu_state 确认 halted=true"
        ]
    }
}


def get_usage_guidance(category: str | None = None, include_examples: bool = True) -> Dict[str, Any]:
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
        包含以下字段的字典:
        - success: 是否成功
        - total_tools: 工具总数
        - categories: 工具分类列表
        - tools: 工具详情列表（按分类）
        - scenarios: 常见使用场景
        - quick_start: 快速开始步骤
    """
    try:
        # 统计总工具数
        total_tools = sum(len(cat_info["tools"]) for cat_info in TOOL_CATEGORIES.values())

        # 筛选分类
        if category:
            category_lower = category.lower()
            filtered_categories = {
                k: v for k, v in TOOL_CATEGORIES.items()
                if k.lower() == category_lower or category_lower in k.lower()
            }
        else:
            filtered_categories = TOOL_CATEGORIES

        # 构建工具详情
        tools_detail = {}
        for cat_name, cat_info in filtered_categories.items():
            tools_detail[cat_name] = {
                "description": cat_info["description"],
                "tool_count": len(cat_info["tools"]),
                "tools": cat_info["tools"]
            }

        # 快速开始指南
        quick_start = [
            "1. 调用 get_usage_guidance() 查看可用工具",
            "2. 调用 list_jlink_devices() 列出设备",
            "3. 调用 connect_device() 连接设备",
            "4. 调用 get_connection_status() 确认连接",
            "5. 根据需要调用其他工具执行操作"
        ]

        logger.info(f"获取使用指南: category={category}, tools={total_tools}")

        return {
            "success": True,
            "total_tools": total_tools,
            "categories": list(TOOL_CATEGORIES.keys()),
            "tools": tools_detail,
            "scenarios": USAGE_SCENARIOS if include_examples else {},
            "quick_start": quick_start
        }
    except Exception as e:
        logger.error(f"获取使用指南失败: {e}")
        return {
            "success": False,
            "error": str(e),
            "suggestion": "请检查参数格式或联系开发者"
        }


def get_best_practices(task_type: str) -> Dict[str, Any]:
    """获取指定任务类型的最佳实践.

    Args:
        task_type: 任务类型，支持：
            - read_registers: 读取寄存器
            - connect_device: 连接设备
            - memory_operations: 内存操作
            - flash_operations: Flash 操作
            - debug: 调试控制

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - task_type: 任务类型
        - title: 最佳实践标题
        - recommended_flow: 推荐流程步骤
        - forbidden: 禁止的操作
        - performance_tips: 性能优化建议
        - common_mistakes: 常见错误和正确做法
        - troubleshooting: 故障排除指南（如果适用）
    """
    try:
        task_type_lower = task_type.lower()

        # 查找最佳实践
        practices = None
        for key, value in BEST_PRACTICES.items():
            if key.lower() == task_type_lower or task_type_lower in key.lower():
                practices = value
                break

        if not practices:
            # 提供可用的任务类型建议
            available_types = list(BEST_PRACTICES.keys())
            return {
                "success": False,
                "error": f"未找到任务类型 '{task_type}' 的最佳实践",
                "available_types": available_types,
                "suggestion": f"请使用以下任务类型之一: {', '.join(available_types)}"
            }

        logger.info(f"获取最佳实践: task_type={task_type}")

        return {
            "success": True,
            "task_type": task_type,
            **practices
        }
    except Exception as e:
        logger.error(f"获取最佳实践失败: {e}")
        return {
            "success": False,
            "error": str(e),
            "suggestion": "请检查任务类型或联系开发者"
        }


def list_scenarios() -> Dict[str, Any]:
    """列出所有可用的使用场景.

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - scenarios: 场景列表
        - total_scenarios: 场景总数
    """
    try:
        scenarios_list = []
        for name, info in USAGE_SCENARIOS.items():
            scenarios_list.append({
                "name": name,
                "description": info["description"],
                "steps_count": len(info["steps"]),
                "expected_time": info.get("expected_time", "未知")
            })

        logger.info(f"列出使用场景: {len(scenarios_list)} 个场景")

        return {
            "success": True,
            "scenarios": scenarios_list,
            "total_scenarios": len(scenarios_list)
        }
    except Exception as e:
        logger.error(f"列出使用场景失败: {e}")
        return {
            "success": False,
            "error": str(e)
        }


def get_forbidden_operations() -> Dict[str, Any]:
    """获取禁止的操作列表.

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - forbidden: 禁止操作列表
        - reasons: 禁止原因说明
    """
    forbidden_ops = {
        "tool_usage": [
            "不要重复调用已失败的连接（先分析错误，再换用不同方法）",
            "不要调用 get_svd_peripherals() 遍历所有外设来查找地址",
            "不要在已知地址的情况下重复调用 get_svd_registers() 获取寄存器列表"
        ],
        "flash": [
            "不要用 write_memory 代替 program_flash 烧录 Flash",
            "不要烧录到非 Flash 地址（如 RAM / 外设区）",
            "不要在烧录失败后直接运行 CPU（先确认烧录完整性）"
        ],
        "cpu": [
            "CPU 寄存器需要稳定暂停；普通内存和外设读取不会自动暂停",
            "不要在 CPU 运行时设置断点（先 halt_cpu）"
        ],
        "performance": [
            "不要在循环中重复调用相同的工具",
            "不要一次性读取大量数据（超过 64KB）",
            "不要盲目调用 get_svd_peripherals() 全量遍历外设"
        ]
    }

    return {
        "success": True,
        "forbidden": forbidden_ops,
        "reasons": {
            "tool_usage": "优化工具调用顺序可提高性能和用户体验",
            "flash": "Flash 只能按扇区整片擦写，操作顺序错误会损坏数据",
            "cpu": "一致快照需要暂停；实时观察内存允许保持运行",
            "performance": "避免不必要操作可显著提升响应速度"
        }
    }