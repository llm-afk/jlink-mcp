"""JLink 设备管理器 - 单例模式管理 JLink 连接."""

import pylink
from typing import Optional, List
from contextlib import contextmanager

from .exceptions import (
    JLinkMCPError,
    JLinkErrorCode,
    DeviceNotFoundError,
    ConnectionError,
    OperationError
)
from .models.device import (
    DeviceInfo,
    ConnectionStatus,
    TargetDeviceInfo,
    TargetInterface
)
from .utils import logger, find_jlink_dll
from .device_patch_manager import device_patch_manager


class JLinkManager:
    """JLink 设备管理器（单例模式）.

    管理单个 JLink 设备的连接、断开和状态查询。
    确保同一时间只有一个连接存在。
    """

    _instance: Optional["JLinkManager"] = None
    _initialized: bool = False

    def __new__(cls) -> "JLinkManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if JLinkManager._initialized:
            return

        self._jlink: Optional[pylink.JLink] = None
        self._connected: bool = False
        self._device_serial: Optional[str] = None
        self._target_interface: TargetInterface = TargetInterface.SWD
        self._target_connected: bool = False
        self._device_name: Optional[str] = None

        JLinkManager._initialized = True
        logger.debug("JLinkManager 初始化完成")

    @property
    def is_connected(self) -> bool:
        """检查是否已连接到 JLink 设备."""
        if self._jlink is None:
            return False
        try:
            # 尝试访问设备属性来验证连接
            _ = self._jlink.serial_number
            return True
        except Exception:
            return False

    @property
    def is_target_connected(self) -> bool:
        """检查目标芯片是否已连接."""
        if not self.is_connected or self._jlink is None:
            return False
        try:
            return self._jlink.target_connected()
        except Exception:
            return False

    def enumerate_devices(self) -> List[DeviceInfo]:
        """枚举所有连接的 JLink 设备.

        Returns:
            设备信息列表
        """
        devices = []
        try:
            # 获取 USB 设备列表
            dll_path = find_jlink_dll()
            lib = pylink.Library(dllpath=dll_path) if dll_path else None
            probe = pylink.JLink(lib=lib) if lib else pylink.JLink()
            usb_devices = probe.connected_emulators()
            for dev in usb_devices:
                # 安全地获取设备属性
                serial_number = getattr(dev, 'SerialNumber', 'Unknown')
                product_name = getattr(dev, 'ProductName', None) or getattr(dev, 'ProductName', 'J-Link')
                if product_name is None:
                    product_name = "J-Link"

                device_info = DeviceInfo(
                    serial_number=str(serial_number),
                    product_name=product_name,
                    firmware_version="Unknown",
                    connection_type="USB",
                    hardware_version=None
                )
                devices.append(device_info)

            logger.info(f"发现 {len(devices)} 个 JLink 设备")
            return devices

        except Exception as e:
            logger.error(f"枚举设备失败: {e}")
            return []

    def connect(
        self,
        serial_number: Optional[str] = None,
        interface: TargetInterface = TargetInterface.SWD,
        chip_name: Optional[str] = None,
        jlink_path: Optional[str] = None
    ) -> None:
        """连接到 JLink 设备.

        Args:
            serial_number: 设备序列号，None 则连接第一个可用设备
            interface: 目标接口类型（SWD/JTAG）
            chip_name: 目标芯片名称（如 STM32F407VG），None 则尝试自动检测
            jlink_path: 指定 JLink 安装目录或 JLink_x64.dll 文件路径（可选，
                如 D:\\Program Files\\SEGGER\\JLink_V942）；None 则自动选择最新版本

        Raises:
            AlreadyConnectedError: 如果已连接
            DeviceNotFoundError: 如果未找到设备
            ConnectionError: 如果连接失败
        """
        if self.is_connected:
            raise JLinkMCPError(
                JLinkErrorCode.ALREADY_CONNECTED,
                f"已连接到设备 {self._device_serial}"
            )

        try:
            # 定位 JLink DLL：显式指定 jlink_path 优先，否则自动扫描最新版本
            import os
            lib = None
            if jlink_path:
                if os.path.isdir(jlink_path):
                    candidate = os.path.join(jlink_path, "JLink_x64.dll")
                    dll_path = candidate if os.path.isfile(candidate) else None
                elif os.path.isfile(jlink_path):
                    dll_path = jlink_path
                else:
                    dll_path = None
            else:
                dll_path = find_jlink_dll()
            if dll_path:
                try:
                    lib = pylink.Library(dllpath=dll_path)
                    logger.info(f"使用 JLink DLL: {dll_path}")
                except Exception as e:
                    logger.warning(f"加载 JLink DLL 失败（回退默认查找）: {dll_path} - {e}")
                    lib = None
            self._jlink = pylink.JLink(lib=lib) if lib else pylink.JLink()

            # 打开设备
            if serial_number:
                logger.info(f"正在连接设备: {serial_number}")
                self._jlink.open(serial_no=serial_number)
            else:
                logger.info("正在连接第一个可用设备")
                self._jlink.open()

            # 禁用 J-Link 对话框弹窗（erase/flash 等操作时不再弹出 GUI 窗口）
            try:
                self._jlink.disable_dialog_boxes()
            except Exception as e:
                logger.warning(f"禁用对话框失败（可忽略）: {e}")

            # 设置接口类型
            self._target_interface = interface
            if interface == TargetInterface.SWD:
                self._jlink.set_tif(pylink.JLinkInterfaces.SWD)
            elif interface == TargetInterface.JTAG:
                self._jlink.set_tif(pylink.JLinkInterfaces.JTAG)
            else:
                raise ConnectionError(f"不支持的接口类型: {interface}")

            # 连接目标芯片
            if chip_name:
                # 优先直接用 pylink 原生设备数据库连接（支持 GD32/STM32/Cortex-M 等）
                logger.info(f"连接到芯片: {chip_name}")
                try:
                    self._jlink.connect(chip_name)
                except Exception as conn_err:
                    # 原生数据库失败时，尝试设备补丁智能匹配（如 Flagchip 自定义设备）
                    suggestion = ""
                    try:
                        match_result = device_patch_manager.match_device_name(chip_name)
                    except Exception:
                        match_result = None
                    if match_result and match_result[0] and match_result[0] != chip_name:
                        matched_name = match_result[0]
                        logger.info(f"设备名称智能匹配: {chip_name} -> {matched_name}")
                        try:
                            self._jlink.connect(matched_name)
                            chip_name = matched_name
                        except Exception as retry_err:
                            raise ConnectionError(
                                f"无法连接到芯片 '{chip_name}'（已尝试匹配 {matched_name}）",
                                retry_err
                            )
                    else:
                        similar = device_patch_manager.find_similar_devices(chip_name, limit=3)
                        if similar:
                            suggestion = f"\n您是否想找: {', '.join(similar)}"
                        raise ConnectionError(
                            f"无法连接到芯片 '{chip_name}'{suggestion}",
                            conn_err
                        )
                self._device_name = chip_name
            else:
                self._device_name = self._autodetect_chip()

            self._target_connected = self._jlink.target_connected()

            self._connected = True
            self._device_serial = str(self._jlink.serial_number)

            logger.info(f"成功连接到设备: {self._device_serial}")

        except Exception as e:
            self._cleanup()
            if "not found" in str(e).lower():
                raise DeviceNotFoundError(f"设备 {serial_number} 未找到", e)
            raise ConnectionError(str(e), e)

    def _autodetect_chip(self) -> str:
        """自动检测芯片：依次尝试通用内核名、常见设备名和补丁设备名.

        Returns:
            成功连接的芯片名称。

        Raises:
            ConnectionError: 如果所有候选芯片均连接失败。
        """
        logger.info("尝试自动检测芯片...")
        candidates = [
            # 通用内核名（pylink 原生支持，任何同内核芯片均可连接）
            "Cortex-M4", "Cortex-M3", "Cortex-M0", "Cortex-M7", "Cortex-M0+",
            # 常见 GD32 / STM32 设备
            "GD32C103VB", "GD32C103RB", "GD32C103TB", "GD32C103CB",
            "STM32F407VG", "STM32F103C8", "STM32L431RC",
            "nRF52832_xxAA", "MK64FN1M0xxx12",
        ]
        # 追加设备补丁中的设备名（如 Flagchip 等自定义设备）
        try:
            for dev in device_patch_manager.get_all_device_names():
                if dev not in candidates:
                    candidates.append(dev)
        except Exception as e:
            logger.warning(f"获取设备补丁列表失败: {e}")

        last_err = None
        for chip in candidates:
            try:
                logger.info(f"尝试芯片: {chip}")
                self._jlink.connect(chip)
                logger.info(f"成功连接到芯片: {chip}")
                return chip
            except Exception as e:
                last_err = e
                continue

        raise ConnectionError(
            f"无法自动检测芯片，请手动指定 chip_name。\n"
            f"已尝试: {candidates[:20]}",
            last_err
        )

    def disconnect(self) -> None:
        """断开 JLink 连接."""
        if not self.is_connected:
            logger.debug("没有活动连接，仍清理旧会话状态")

        logger.info("正在断开连接")
        self._cleanup()
        logger.info("连接已断开")

    def _cleanup(self) -> None:
        """清理资源."""
        from .tools.rtt import reset_rtt_state
        reset_rtt_state(self._jlink)
        if self._jlink:
            try:
                self._jlink.close()
            except Exception as e:
                logger.warning(f"关闭连接时出错: {e}")
            finally:
                self._jlink = None

        self._connected = False
        self._device_serial = None
        self._target_connected = False
        self._device_name = None

    def get_connection_status(self) -> ConnectionStatus:
        """获取连接状态.

        Returns:
            连接状态信息
        """
        if not self.is_connected or self._jlink is None:
            return ConnectionStatus(
                connected=False,
                device_serial=None,
                target_interface=None,
                target_voltage=None,
                target_connected=False,
                firmware_version=None
            )

        try:
            status = self._jlink.hardware_status  # 属性不是方法，去掉括号
            voltage = status.VTarget / 1000.0  # 转换 mV → V
            fw_version = self._jlink.version
        except Exception as e:
            # 记录异常信息用于诊断
            logger.warning(f"获取硬件状态失败: {e}")
            voltage = None
            fw_version = None

        # 确保 device_serial 有有效值
        device_serial = self._device_serial
        if device_serial is None:
            try:
                device_serial = self._jlink.serial_number
            except Exception as e:
                logger.warning(f"获取设备序列号失败: {e}")
                device_serial = "Unknown"

        return ConnectionStatus(
            connected=True,
            device_serial=device_serial,
            target_interface=self._target_interface,
            target_voltage=voltage,
            target_connected=self.is_target_connected,
            firmware_version=fw_version
        )

    def get_target_info(self) -> TargetDeviceInfo:
        """获取目标设备信息.

        Returns:
            目标设备信息

        Raises:
            JLinkMCPError: 如果未连接或获取失败
        """
        self._ensure_connected()
        self._ensure_target_connected()

        try:
            jlink = self._jlink

            # 设备名称：使用连接时保存的名称（pylink-square 无 device_name()）
            device_name = getattr(self, '_device_name', None)

            # 获取内核信息
            core_type = None
            core_id = None
            try:
                core_type = jlink.core_name()
                core_id = jlink.core_id()
            except Exception:
                pass

            # 设备 ID：pylink-square 无 device_id()，用 core_id 代替
            device_id = core_id

            # 获取 Flash 和 RAM 信息
            flash_size = None
            ram_size = None
            ram_addresses = []

            try:
                # 尝试从设备信息获取内存布局
                if hasattr(jlink, 'device'):
                    device = jlink.device
                    if device:
                        flash_size = device.FlashSize
                        ram_size = device.RAMSize
                        # RAM 地址范围可能需要从设备数据库获取
            except Exception:
                pass

            return TargetDeviceInfo(
                device_name=device_name,
                core_type=core_type,
                core_id=core_id,
                device_id=device_id,
                flash_size=flash_size,
                ram_size=ram_size,
                ram_addresses=ram_addresses
            )

        except JLinkMCPError:
            raise
        except Exception as e:
            raise OperationError(
                JLinkErrorCode.READ_FAILED,
                f"获取目标信息失败: {e}",
                e
            )

    def get_jlink(self) -> pylink.JLink:
        """获取 JLink 实例.

        Returns:
            JLink 实例

        Raises:
            JLinkMCPError: 如果未连接
        """
        self._ensure_connected()
        return self._jlink

    def _ensure_connected(self) -> None:
        """确保已连接到 JLink 设备.

        Raises:
            JLinkMCPError: 如果未连接
        """
        if not self.is_connected:
            raise JLinkMCPError(
                JLinkErrorCode.NOT_INITIALIZED,
                "JLink 未连接"
            )

    def _ensure_target_connected(self) -> None:
        """确保目标芯片已连接.

        Raises:
            JLinkMCPError: 如果目标未连接
        """
        if not self.is_target_connected:
            raise JLinkMCPError(
                JLinkErrorCode.TARGET_NOT_CONNECTED,
                "目标芯片未连接"
            )

    @contextmanager
    def session(self):
        """上下文管理器，用于自动管理连接.

        示例:
            with jlink_manager.session():
                # 执行操作
                pass
        """
        try:
            yield self
        finally:
            if self.is_connected:
                self.disconnect()


# 全局单例实例
jlink_manager = JLinkManager()
