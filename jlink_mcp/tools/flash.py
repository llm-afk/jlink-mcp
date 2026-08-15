"""Flash 操作工具函数."""

from typing import Dict, Any, Optional

from ..jlink_manager import jlink_manager
from ..exceptions import JLinkMCPError, JLinkErrorCode
from ..models.operations import FlashEraseRequest, FlashProgramRequest
from ..utils import logger, human_readable_size, parse_hex_string


def erase_flash(
    start_address: Optional[int] = None,
    end_address: Optional[int] = None,
    chip_erase: bool = False
) -> Dict[str, Any]:
    """擦除 Flash.

    Args:
        start_address: 起始地址（可选）
        end_address: 结束地址（可选）
        chip_erase: 是否整片擦除（默认 False）

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - erase_type: 擦除类型（chip/sector）
        - bytes_erased: 擦除的字节数
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()

        if chip_erase:
            # 整片擦除
            logger.info("执行整片擦除")
            jlink.erase()
            erase_type = "chip"
            bytes_erased = 0  # 整片擦除无法知道具体字节数
        elif start_address is not None and end_address is not None:
            # 指定范围擦除
            if start_address >= end_address:
                raise JLinkMCPError(
                    JLinkErrorCode.INVALID_PARAMETER,
                    f"起始地址 {start_address:#x} 必须小于结束地址 {end_address:#x}"
                )

            size = end_address - start_address
            logger.info(f"擦除 Flash {start_address:#x} - {end_address:#x} ({human_readable_size(size)})")

            # pylink-square 无分扇区擦除接口，只能整片擦除
            jlink.erase()
            erase_type = "chip"
            bytes_erased = 0  # 整片擦除无法知道具体字节数
        else:
            raise JLinkMCPError(
                JLinkErrorCode.INVALID_PARAMETER,
                "必须指定 chip_erase=True 或提供 start_address 和 end_address"
            )

        logger.info("Flash 擦除成功")
        message = f"Flash 擦除成功（{erase_type}）"
        if erase_type == "chip":
            message += " ⚠️ 已整片擦除整个 Flash（含 bootloader）；如需只擦指定扇区请改用 erase_sector"
        return {
            "success": True,
            "erase_type": erase_type,
            "bytes_erased": bytes_erased,
            "message": message
        }
    except JLinkMCPError as e:
        logger.error(f"擦除 Flash 失败: {e}")
        return {
            "success": False,
            "erase_type": None,
            "bytes_erased": 0,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"擦除 Flash 失败: {e}")
        return {
            "success": False,
            "erase_type": None,
            "bytes_erased": 0,
            "error": {
                "code": JLinkErrorCode.ERASE_FAILED.value[0],
                "description": str(e),
                "suggestion": "请检查 Flash 是否被保护，尝试先解除保护"
            }
        }


def erase_sector(address: int, count: int = 1, page_size: int = 1024) -> Dict[str, Any]:
    """按扇区（页）擦除 Flash.

    通过 J-Link 原生 Flash 下载算法实现扇区擦除：向目标区域写入全 0xFF。
    J-Link 会按所连接芯片的 Flash 扇区布局先擦除目标扇区，写入 0xFF（即
    擦除态）等效于擦除，从而只清除指定扇区、不触碰其余 Flash。

    通用性说明：本接口不依赖任何具体芯片的寄存器，而是复用 J-Link 的设备
    Flash 算法（RAM 驻留 loader，即 pylink 的 ``flash()``），因此对 J-Link
    支持的 Cortex-M 芯片（GD32 / STM32 / nRF 等）均通用。``page_size`` 仅
    用于对齐与计算擦除字节数，实际擦除粒度由 J-Link 按设备扇区大小决定。

    Args:
        address: 要擦除区域内的任意地址（会自动对齐到 page_size 边界）
        count: 连续擦除的页数（默认 1）
        page_size: 页大小（字节，默认 1024）

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - start_address: 对齐后的擦除起始地址
        - end_address: 擦除结束地址
        - bytes_erased: 擦除的字节数
        - pages_erased: 擦除的页数
        - page_size: 使用的页大小
        - message: 状态信息
    """
    try:
        if count <= 0:
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, "count 必须 >= 1")
        if page_size <= 0 or (page_size & (page_size - 1)) != 0:
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, "page_size 必须是 2 的幂")

        jlink = jlink_manager.get_jlink()

        # 对齐到页边界
        page_mask = ~(page_size - 1)
        start_address = address & page_mask
        end_address = start_address + count * page_size
        total_bytes = count * page_size

        logger.info(f"按扇区擦除 Flash: {start_address:#x} - {end_address:#x} "
                    f"({count} 页 × {page_size} B)")

        # 复用 J-Link Flash 算法：下载全 0xFF 到目标扇区（内部先擦除再写入）
        jlink.flash(b"\xff" * total_bytes, start_address)

        # 读回校验：目标区域应全为 0xFF（分块读取，避免单次读取过大）
        chunk = 0x10000  # 64KB
        for off in range(0, total_bytes, chunk):
            n = min(chunk, total_bytes - off)
            read_back = bytes(jlink.memory_read(start_address + off, n))
            if not all(b == 0xFF for b in read_back):
                raise JLinkMCPError(
                    JLinkErrorCode.ERASE_FAILED,
                    f"扇区擦除校验失败（地址 {start_address + off:#x} 仍残留数据）"
                )

        logger.info("扇区擦除成功")
        return {
            "success": True,
            "start_address": start_address,
            "end_address": end_address,
            "bytes_erased": total_bytes,
            "pages_erased": count,
            "page_size": page_size,
            "message": f"成功擦除 {count} 页（{human_readable_size(total_bytes)}）"
        }
    except JLinkMCPError as e:
        logger.error(f"扇区擦除失败: {e}")
        return {
            "success": False,
            "start_address": None,
            "end_address": None,
            "bytes_erased": 0,
            "pages_erased": 0,
            "page_size": page_size,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"扇区擦除失败: {e}")
        return {
            "success": False,
            "start_address": None,
            "end_address": None,
            "bytes_erased": 0,
            "pages_erased": 0,
            "page_size": page_size,
            "error": {
                "code": JLinkErrorCode.ERASE_FAILED.value[0],
                "description": str(e),
                "suggestion": "请确认目标芯片带 FMC 控制器且 Flash 未写保护"
            }
        }


def program_flash(address: int, data: str | None = None, verify: bool = True, file_path: str | None = None) -> Dict[str, Any]:
    """烧录固件到 Flash.

    Args:
        address: 起始地址
        data: 要烧录的数据
        verify: 烧录后是否校验（默认 True）

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - bytes_programmed: 烧录的字节数
        - verify_result: 校验结果（如果 verify=True）
        - message: 状态信息
    """
    try:
        # 解析烧录数据：优先从文件读取，否则解析 hex 字符串
        if file_path:
            with open(file_path, 'rb') as f:
                data_bytes = f.read()
        elif data:
            data_bytes = parse_hex_string(data)
        else:
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, "必须提供 data 或 file_path")

        jlink = jlink_manager.get_jlink()

        logger.info(f"烧录 Flash {address:#x} 大小 {human_readable_size(len(data_bytes))}")
        jlink.flash(data_bytes, address)

        verify_result = None
        if verify:
            # 校验
            logger.info("校验 Flash")
            read_back = jlink.memory_read(address, len(data_bytes))
            read_back_bytes = bytes(read_back)  # 统一为 bytes，避免 list/bytes 比较恒为 False

            if read_back_bytes == data_bytes:
                verify_result = {"matched": True, "mismatches": []}
                logger.info("Flash 校验成功")
            else:
                # 找出不匹配的位置
                mismatches = []
                for i, (a, b) in enumerate(zip(data_bytes, read_back_bytes)):
                    if a != b:
                        mismatches.append({"address": address + i, "expected": a, "actual": b})

                verify_result = {"matched": False, "mismatches": mismatches}
                logger.warning(f"Flash 校验失败，{len(mismatches)} 处不匹配")

        logger.info("Flash 烧录成功")
        return {
            "success": True,
            "bytes_programmed": len(data_bytes),
            "verify_result": verify_result,
            "message": f"成功烧录 {human_readable_size(len(data_bytes))} 到 Flash"
        }
    except JLinkMCPError as e:
        logger.error(f"烧录 Flash 失败: {e}")
        return {
            "success": False,
            "bytes_programmed": 0,
            "verify_result": None,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"烧录 Flash 失败: {e}")
        return {
            "success": False,
            "bytes_programmed": 0,
            "verify_result": None,
            "error": {
                "code": JLinkErrorCode.VERIFY_FAILED.value[0],
                "description": str(e),
                "suggestion": "请检查 Flash 是否已擦除，地址是否有效"
            }
        }


def verify_flash(address: int, data: str) -> Dict[str, Any]:
    """校验 Flash 内容.

    Args:
        address: 起始地址
        data: 期望的数据

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - matched: 数据是否匹配
        - mismatches: 不匹配地址列表
        - message: 状态信息
    """
    try:
        if not data:
            raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, "数据不能为空")

        data_bytes = parse_hex_string(data)

        jlink = jlink_manager.get_jlink()
        read_back = jlink.memory_read(address, len(data_bytes))
        read_back_bytes = bytes(read_back)  # 统一为 bytes，避免 list/bytes 比较恒为 False

        if read_back_bytes == data_bytes:
            logger.info(f"Flash 校验成功（{len(data_bytes)} 字节）")
            return {
                "success": True,
                "matched": True,
                "mismatches": [],
                "message": f"Flash 校验成功（{len(data_bytes)} 字节）"
            }
        else:
            # 找出不匹配的位置
            mismatches = []
            for i, (a, b) in enumerate(zip(data_bytes, read_back_bytes)):
                if a != b:
                    mismatches.append({
                        "address": address + i,
                        "expected": a,
                        "actual": b
                    })

            logger.warning(f"Flash 校验失败，{len(mismatches)} 处不匹配")
            return {
                "success": True,
                "matched": False,
                "mismatches": mismatches,
                "message": f"Flash 校验失败，{len(mismatches)} 处不匹配"
            }
    except JLinkMCPError as e:
        logger.error(f"校验 Flash 失败: {e}")
        return {
            "success": False,
            "matched": False,
            "mismatches": [],
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"校验 Flash 失败: {e}")
        return {
            "success": False,
            "matched": False,
            "mismatches": [],
            "error": {
                "code": JLinkErrorCode.VERIFY_FAILED.value[0],
                "description": str(e),
                "suggestion": "请检查地址是否有效"
            }
        }