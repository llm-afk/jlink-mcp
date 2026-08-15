"""全局配置管理器.

管理服务器的全局配置，包括默认参数、提示词模板等。
"""

from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

from .utils import logger


class ServerConfig(BaseModel):
    """服务器配置."""
    default_interface: str = Field(default="JTAG", description="默认接口类型")
    default_timeout_ms: int = Field(default=10000, description="默认超时时间（毫秒）")
    enable_auto_detect: bool = Field(default=True, description="是否启用自动检测")
    max_memory_read_size: int = Field(default=65536, description="最大内存读取大小（字节）")
    system_prompt: Optional[str] = Field(default=None, description="系统提示词")
    custom_prompts: Dict[str, str] = Field(default_factory=dict, description="自定义提示词字典")


class ConfigManager:
    """配置管理器（单例模式）.

    管理服务器全局配置，包括：
    - 系统提示词（AI 行为指导）
    - 自定义提示词（场景化指导）
    - 服务器参数配置
    """

    _instance: Optional["ConfigManager"] = None
    _initialized: bool = False

    def __new__(cls) -> "ConfigManager":
        """单例模式实现."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化配置管理器."""
        if ConfigManager._initialized:
            return

        self._config = ServerConfig()

        # 初始化默认系统提示词
        self._config.system_prompt = self._get_default_system_prompt()

        ConfigManager._initialized = True
        logger.debug("ConfigManager 初始化完成")

    def get_config(self) -> ServerConfig:
        """获取当前配置.

        Returns:
            服务器配置对象
        """
        return self._config

    def update_config(self, **kwargs) -> None:
        """更新配置.

        Args:
            **kwargs: 配置键值对
        """
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)
                logger.info(f"配置更新: {key} = {value}")
            else:
                logger.warning(f"无效的配置项: {key}")

    def get_system_prompt(self) -> str:
        """获取系统提示词.

        Returns:
            系统提示词内容
        """
        return self._config.system_prompt or ""

    def set_system_prompt(self, prompt: str) -> None:
        """设置系统提示词.

        Args:
            prompt: 系统提示词内容
        """
        self._config.system_prompt = prompt
        logger.info(f"系统提示词已更新: {len(prompt)} 字符")

    def add_custom_prompt(self, name: str, prompt: str) -> None:
        """添加自定义提示词.

        Args:
            name: 提示词名称
            prompt: 提示词内容
        """
        self._config.custom_prompts[name] = prompt
        logger.info(f"自定义提示词已添加: {name}")

    def get_custom_prompt(self, name: str) -> Optional[str]:
        """获取自定义提示词.

        Args:
            name: 提示词名称

        Returns:
            提示词内容，不存在则返回 None
        """
        return self._config.custom_prompts.get(name)

    def list_custom_prompts(self) -> Dict[str, str]:
        """列出所有自定义提示词.

        Returns:
            自定义提示词字典
        """
        return self._config.custom_prompts.copy()

    def remove_custom_prompt(self, name: str) -> bool:
        """移除自定义提示词.

        Args:
            name: 提示词名称

        Returns:
            是否成功移除
        """
        if name in self._config.custom_prompts:
            del self._config.custom_prompts[name]
            logger.info(f"自定义提示词已移除: {name}")
            return True
        return False

    def _get_default_system_prompt(self) -> str:
        """获取默认系统提示词.

        Returns:
            默认系统提示词
        """
        return """你是 JLink MCP 调试专家助手，精通嵌入式系统调试与 J-Link 工具使用。

## 你的职责
1. 帮助用户连接与调试 J-Link 目标设备
2. 提供芯片识别、接口选择与配置建议
3. 指导内存读写、Flash 烧录与寄存器解析
4. 诊断并解决调试过程中的问题，给出明确可操作的建议

## 🎯 全局默认配置
- **默认接口**：SWD（GD32 / STM32 等 Cortex-M 系列最常用，接线少、速度快）
- **芯片名称**：直接使用芯片型号（如 GD32C103VB、STM32F407VG），或用通用内核名（Cortex-M4 等）自动检测
- **暂停规则**：读取寄存器 / 内存前先 halt_cpu()，操作完成后按需 run_cpu()
- **SVD 解析**：读取外设寄存器字段用 read_register_with_fields，不必手动查地址

## ✅ 推荐流程
### 连接设备
1. list_jlink_devices() - 枚举调试器（可选）
2. connect_device(chip_name="GD32C103VB", interface="SWD") - 连接目标
3. get_target_info() - 确认芯片 / 内核 / 电压

### 读取外设寄存器（带字段解析）
1. connect_device(...) - 连接
2. halt_cpu() - 暂停 CPU
3. get_svd_registers(device, peripheral) - 查寄存器定义（仅一次并缓存）
4. read_register_with_fields(device, peripheral, register) - 读取并解析字段

### 内存读写
1. halt_cpu()
2. read_memory(address, size) / write_memory(address, data)

### Flash 操作（整片擦除会清空 bootloader，先确认可重烧）
1. erase_flash(chip_erase=True) - 擦除
2. program_flash(address, data, verify=True) - 烧录并校验

## 📍 地址获取规则
- 已知寄存器地址：直接 read_register_by_address(address)
- 未知地址：调用一次 get_svd_registers(device, peripheral) 并缓存
- 避免遍历 get_svd_peripherals 全量外设来查地址

## 📋 错误处理原则
- 连接失败：检查芯片名、接口类型（SWD/JTAG）、目标是否上电
- 读取失败：检查是否已 halt_cpu()；目标运行中会自动暂停
- Flash 失败：先擦除再烧录
- 始终提供具体诊断与可操作建议

## 💡 性能与体验
- 无依赖的工具调用尽量并行
- 缓存查询结果（外设 / 寄存器 / 地址）
- 只读取必要数据，减少传输
- 使用 get_usage_guidance() / get_best_practices() 获取最佳实践
"""


# 全局单例实例
config_manager = ConfigManager()