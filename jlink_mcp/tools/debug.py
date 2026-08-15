"""调试控制工具函数."""

import time
from typing import Dict, Any, List, Optional

from ..jlink_manager import jlink_manager
from ..exceptions import JLinkMCPError, JLinkErrorCode
from ..models.operations import DebugBreakpoint, CPUState
from ..utils import logger


def _u32(value):
    """将寄存器值规范化为无符号 32 位（避免 PC/LR/SP 显示为负数）."""
    return value & 0xFFFFFFFF if isinstance(value, int) else value


def reset_target(reset_type: str = "normal") -> Dict[str, Any]:
    """复位目标芯片.

    Args:
        reset_type: 复位类型
            - normal: 普通复位
            - halt: 复位后暂停
            - core: 内核复位

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - reset_type: 复位类型
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()

        if reset_type == "halt":
            logger.info("执行复位并暂停")
            jlink.reset(ms=0, halt=True)
        elif reset_type == "core":
            # 内核复位：仅复位内核，不拉硬件复位引脚（外设状态保留）
            logger.info("执行内核复位")
            try:
                import pylink
                jlink.set_reset_strategy(pylink.JLinkResetStrategyCortexM3.CORE)
            except Exception as e:
                logger.warning(f"设置内核复位策略失败（回退普通复位并暂停）: {e}")
            jlink.reset(ms=0, halt=True)
        else:  # normal
            logger.info("执行普通复位")
            jlink.reset(ms=0, halt=False)

        return {
            "success": True,
            "reset_type": reset_type,
            "message": f"目标已复位（{reset_type}）"
        }
    except JLinkMCPError as e:
        logger.error(f"复位失败: {e}")
        return {
            "success": False,
            "reset_type": reset_type,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"复位失败: {e}")
        return {
            "success": False,
            "reset_type": reset_type,
            "error": {
                "code": JLinkErrorCode.RESET_FAILED.value[0],
                "description": str(e),
                "suggestion": "请检查目标芯片连接和供电状态"
            }
        }


def halt_cpu() -> Dict[str, Any]:
    """暂停 CPU.

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - pc: 程序计数器值
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()

        # halt() 是同步的（pylink 的 async_decorator 仅在传入 callback 时才异步），
        # 成功时内部会 sleep 1s 等待目标真正停止
        jlink.halt()

        # 以 halted() 为准轮询确认目标已停止（halt() 返回值在某些 DLL/固件下不可靠）
        halted = False
        for _ in range(200):  # 最长等待约 1s
            if jlink.halted():
                halted = True
                break
            time.sleep(0.005)

        if not halted:
            # 目标未能在预期时间内停止。绝不能在此处 reset 兜底 ——
            # reset 会破坏正在运行的程序状态，并把 PC 重置到复位向量，
            # 导致 "暂停" 被误执行为 "复位"。
            raise JLinkMCPError(
                JLinkErrorCode.HALT_FAILED,
                "目标未能在预期时间内停止，可能处于低功耗模式，或固件使能了"
                "调试期间不停计的看门狗（复位循环）"
            )

        pc = _u32(jlink.register_read("R15 (PC)"))
        logger.info(f"CPU 已暂停，PC = {pc:#x}")

        return {
            "success": True,
            "pc": pc,
            "message": f"CPU 已暂停，PC = {pc:#x}"
        }
    except JLinkMCPError as e:
        logger.error(f"暂停 CPU 失败: {e}")
        return {
            "success": False,
            "pc": None,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"暂停 CPU 失败: {e}")
        return {
            "success": False,
            "pc": None,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请检查目标是否正在运行"
            }
        }


def run_cpu() -> Dict[str, Any]:
    """运行 CPU.

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()
        if jlink.halted():
            # 目标已暂停，restart() 恢复运行
            jlink.restart()
            logger.info("CPU 已开始运行")
            return {
                "success": True,
                "message": "CPU 已开始运行"
            }
        else:
            logger.info("CPU 已经在运行")
            return {
                "success": True,
                "message": "CPU 已经在运行"
            }
    except JLinkMCPError as e:
        logger.error(f"运行 CPU 失败: {e}")
        return {
            "success": False,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"运行 CPU 失败: {e}")
        return {
            "success": False,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请检查目标芯片状态，尝试复位后重试"
            }
        }


def step_instruction() -> Dict[str, Any]:
    """单步执行一条指令.

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - pc: 程序计数器值
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()
        jlink.step(thumb=True)

        pc = _u32(jlink.register_read("R15 (PC)"))
        logger.info(f"单步执行，PC = {pc:#x}")

        return {
            "success": True,
            "pc": pc,
            "message": f"单步执行完成，PC = {pc:#x}"
        }
    except JLinkMCPError as e:
        logger.error(f"单步执行失败: {e}")
        return {
            "success": False,
            "pc": None,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"单步执行失败: {e}")
        return {
            "success": False,
            "pc": None,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请确保目标已暂停"
            }
        }


def get_cpu_state() -> Dict[str, Any]:
    """获取 CPU 状态.

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - running: 是否正在运行
        - halted: 是否已暂停
        - pc: 程序计数器
        - lr: 链接寄存器
        - sp: 堆栈指针
    """
    try:
        jlink = jlink_manager.get_jlink()

        # 检查运行状态
        halted = jlink.halted()
        running = not halted

        pc = lr = sp = None
        if halted:
            try:
                pc = _u32(jlink.register_read("R15 (PC)"))
                lr = _u32(jlink.register_read("R14"))  # LR 寄存器名称是 R14，不是 R14 (LR)
                sp = _u32(jlink.register_read("R13 (SP)"))
            except Exception as e:
                logger.warning(f"读取寄存器失败: {e}")

        logger.info(f"CPU 状态: {'运行' if running else '暂停'}")
        return {
            "success": True,
            "running": running,
            "halted": halted,
            "pc": pc,
            "lr": lr,
            "sp": sp,
            "message": f"CPU 状态: {'运行中' if running else '已暂停'}"
        }
    except JLinkMCPError as e:
        logger.error(f"获取 CPU 状态失败: {e}")
        return {
            "success": False,
            "running": False,
            "halted": False,
            "pc": None,
            "lr": None,
            "sp": None,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"获取 CPU 状态失败: {e}")
        return {
            "success": False,
            "running": False,
            "halted": False,
            "pc": None,
            "lr": None,
            "sp": None,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请检查设备连接状态"
            }
        }


def set_breakpoint(address: int) -> Dict[str, Any]:
    """设置断点.

    Args:
        address: 断点地址

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - address: 断点地址
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()

        # 确保目标已暂停
        if not jlink.halted():
            raise JLinkMCPError(
                JLinkErrorCode.TARGET_RUNNING,
                "目标正在运行，无法设置断点",
                "请先调用 halt_cpu 暂停目标"
            )

        # 设置断点
        jlink.breakpoint_set(address, thumb=True)

        logger.info(f"断点已设置: {address:#x}")
        return {
            "success": True,
            "address": address,
            "message": f"断点已设置: {address:#x}"
        }
    except JLinkMCPError as e:
        logger.error(f"设置断点失败: {e}")
        return {
            "success": False,
            "address": address,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"设置断点失败: {e}")
        return {
            "success": False,
            "address": address,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请检查地址是否有效，是否已达到断点数量限制"
            }
        }


def clear_breakpoint(address: int) -> Dict[str, Any]:
    """清除断点.

    Args:
        address: 断点地址

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - address: 断点地址
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()

        # 查找断点句柄并清除
        handle = jlink.breakpoint_find(address)
        if handle:
            jlink.breakpoint_clear(handle)
            logger.info(f"断点已清除: {address:#x}")
            return {
                "success": True,
                "address": address,
                "message": f"断点已清除: {address:#x}"
            }
        else:
            logger.warning(f"地址 {address:#x} 没有断点")
            return {
                "success": True,
                "address": address,
                "message": f"地址 {address:#x} 没有断点，无需清除"
            }
    except JLinkMCPError as e:
        logger.error(f"清除断点失败: {e}")
        return {
            "success": False,
            "address": address,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"清除断点失败: {e}")
        return {
            "success": False,
            "address": address,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请检查地址是否正确"
            }
        }