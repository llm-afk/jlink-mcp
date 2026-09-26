"""SVD 文件管理器 - 单例模式管理 SVD 解析和查询.

负责加载和解析所有 SVD 文件，提供芯片、外设、寄存器、字段的查询接口。

优化特性:
- 延迟加载: 只在首次访问设备时加载对应的 SVD 文件
- JSON 缓存: 解析后的数据缓存到用户目录
- 索引查找: 外设和寄存器使用字典索引，O(1) 复杂度
- 预计算: 字段 mask 值和枚举字典在解析时预计算
- 缓存: 使用 LRU 缓存频繁查询的结果
"""

from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from functools import lru_cache, wraps
from threading import RLock
from copy import deepcopy
import xml.etree.ElementTree as ET
import json
import hashlib
import os
import tempfile

from .models.svd import (
    DeviceSVD, PeripheralInfo, RegisterInfo, FieldInfo,
    CPUInfo, EnumeratedValue
)
from .utils import logger


def _synchronized(method):
    """元数据与硬件工作线程共享同一 SVD 管理器。"""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class SVDManager:
    """SVD 文件管理器（单例模式）.

    负责加载和解析所有 SVD 文件，提供芯片、外设、寄存器、字段的查询接口。
    使用延迟加载、JSON 缓存和索引优化查询性能。
    """

    _instance: Optional["SVDManager"] = None
    _initialized: bool = False
    _lock = RLock()

    # 缓存版本号，当模型结构变化时需要更新
    CACHE_VERSION = 4

    def __new__(cls) -> "SVDManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    @_synchronized
    def __init__(self):
        if SVDManager._initialized:
            return

        # 优先使用外部SVD目录（通过环境变量）
        external_svd_dir = os.environ.get("JLINK_SVD_DIR")
        if external_svd_dir:
            self._svd_path = Path(external_svd_dir)
            logger.info(f"使用外部SVD目录: {external_svd_dir}")
        else:
            # 获取包目录（src/jlink_mcp）
            current_dir = Path(__file__).resolve().parent
            self._svd_path = current_dir / "tool" / "SVD_V1.5.6"
            logger.info(f"使用包内SVD目录: {self._svd_path}")

        # 缓存不可写时仍允许解析 SVD，不依赖包安装目录的写权限。
        self._cache_dir: Optional[Path] = self._create_cache_dir()

        # 存储已加载的设备 SVD 数据
        self._devices: Dict[str, DeviceSVD] = {}

        # 设备名称到 SVD 文件路径的映射（延迟加载用）
        self._svd_file_map: Dict[str, Path] = {}

        # 外设索引: {device_name: {peripheral_name: PeripheralInfo}}
        self._peripheral_index: Dict[str, Dict[str, PeripheralInfo]] = {}

        # 寄存器索引: {device_name: {peripheral_name: {register_name: RegisterInfo}}}
        self._register_index: Dict[str, Dict[str, Dict[str, RegisterInfo]]] = {}

        # 仅扫描 SVD 文件，不加载内容
        self._scan_svd_files()

        SVDManager._initialized = True
        logger.debug(f"SVDManager 初始化完成，发现 {len(self._svd_file_map)} 个 SVD 文件")

    def _scan_svd_files(self) -> None:
        """扫描 SVD 目录，建立设备名到文件的映射（不解析内容）."""
        if not self._svd_path.exists():
            logger.warning(f"SVD 目录不存在: {self._svd_path}")
            return

        svd_files = list(self._svd_path.glob("*.svd"))
        for svd_file in svd_files:
            # 从文件名提取设备名（去掉 .svd 后缀）
            device_name = svd_file.stem
            self._svd_file_map[device_name] = svd_file

        logger.info(f"扫描发现 {len(self._svd_file_map)} 个 SVD 文件")

    @staticmethod
    def _create_cache_dir() -> Optional[Path]:
        """选择当前用户的缓存目录；创建失败时禁用磁盘缓存。"""
        try:
            if os.name == "nt":
                base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            else:
                base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
            cache_dir = base / "jlink-mcp" / "svd"
            cache_dir.mkdir(parents=True, exist_ok=True)
            return cache_dir
        except (OSError, RuntimeError) as exc:
            logger.warning(f"SVD 磁盘缓存不可用，将直接解析文件: {exc}")
            return None

    def _get_cache_path(self, device_name: str) -> Optional[Path]:
        """缓存按源文件绝对路径隔离，同名的外部 SVD 不共享缓存。"""
        source = self._svd_file_map.get(device_name)
        if self._cache_dir is None or source is None:
            return None
        identity = os.path.normcase(str(source.resolve()))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.v{self.CACHE_VERSION}.json"

    @staticmethod
    def _source_signature(svd_path: Path) -> Dict[str, Any]:
        """内容签名可识别保留修改时间的文件替换。"""
        return {
            "path": os.path.normcase(str(svd_path.resolve())),
            "sha256": hashlib.sha256(svd_path.read_bytes()).hexdigest(),
        }

    def _is_cache_valid(self, svd_path: Path, cache_path: Path) -> bool:
        """检查缓存版本、来源及文件内容是否匹配。"""
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return (cached.get("version") == self.CACHE_VERSION
                    and cached.get("source") == self._source_signature(svd_path))
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def _load_from_cache(self, device_name: str) -> Optional[DeviceSVD]:
        """从 JSON 缓存加载并验证模型，不反序列化可执行对象。"""
        try:
            cache_path = self._get_cache_path(device_name)
            svd_path = self._svd_file_map.get(device_name)
            if cache_path is None or svd_path is None or not cache_path.exists():
                return None
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (cached.get("version") != self.CACHE_VERSION
                    or cached.get("source") != self._source_signature(svd_path)):
                return None
            device = DeviceSVD.model_validate(cached["device"])
            logger.info(f"从缓存加载 SVD: {device_name}")
            return device
        except Exception as e:
            logger.warning(f"加载缓存失败 {device_name}: {e}")
            return None

    def _save_to_cache(self, device_name: str, device: DeviceSVD) -> None:
        """原子写入 JSON，缓存故障不影响查询。"""
        temporary_path = None
        try:
            cache_path = self._get_cache_path(device_name)
            if cache_path is None:
                return
            payload = {
                "version": self.CACHE_VERSION,
                "source": self._source_signature(self._svd_file_map[device_name]),
                "device": device.model_dump(mode="json"),
            }
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self._cache_dir,
                                             prefix="svd-", suffix=".tmp", delete=False) as f:
                temporary_path = Path(f.name)
                json.dump(payload, f, ensure_ascii=False)
            temporary_path.replace(cache_path)
            logger.debug(f"保存缓存: {device_name}")
        except Exception as e:
            logger.warning(f"保存缓存失败 {device_name}: {e}")
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @_synchronized
    def clear_cache_dir(self) -> None:
        """清除本版本的磁盘缓存，不删除目录中的其他文件。"""
        if self._cache_dir is not None:
            try:
                for path in self._cache_dir.glob(f"*.v{self.CACHE_VERSION}.json"):
                    path.unlink(missing_ok=True)
                logger.info("已清除所有 SVD 缓存")
            except OSError as exc:
                logger.warning(f"清除 SVD 缓存失败: {exc}")

    def _ensure_device_loaded(self, device_name: str) -> bool:
        """确保指定设备的 SVD 已加载.

        Args:
            device_name: 设备名称

        Returns:
            是否成功加载或已存在
        """
        # 如果设备已加载，直接返回成功
        if device_name in self._devices:
            return True

        # 尝试从 SVD 文件映射中加载
        svd_file = self._svd_file_map.get(device_name)
        if svd_file is None:
            # 尝试模糊匹配设备名
            matched_name = self._find_matching_device(device_name)
            if matched_name:
                device_name = matched_name
                if device_name in self._devices:
                    return True
                svd_file = self._svd_file_map.get(device_name)
            else:
                # 设备不在文件映射中，但可能已通过其他方式添加（如测试）
                # 检查是否有类似的设备名已加载
                for loaded_name in self._devices:
                    if loaded_name.lower() == device_name.lower():
                        return True
                logger.warning(f"未找到设备 {device_name} 的 SVD 文件")
                return False

        try:
            # 1. 尝试从缓存加载
            device = self._load_from_cache(device_name)
            
            if device is None:
                # 2. 解析 SVD 文件
                logger.info(f"延迟加载 SVD 文件: {device_name}")
                device = self._parse_svd_file(svd_file)
                
                if device:
                    # 3. 保存到缓存
                    self._save_to_cache(device_name, device)
            
            if device:
                self._devices[device_name] = device
                self._build_index(device_name, device)
                return True
        except Exception as e:
            logger.error(f"加载 SVD 文件失败 {svd_file.name}: {e}")

        return False

    def _find_matching_device(self, partial_name: str) -> Optional[str]:
        """模糊匹配设备名称.

        Args:
            partial_name: 部分设备名称

        Returns:
            匹配到的完整设备名称，或 None
        """
        partial_lower = partial_name.lower()

        # 1. 精确匹配（忽略大小写）
        for name in self._svd_file_map:
            if name.lower() == partial_lower:
                return name

        # 2. 前缀匹配
        matches = [name for name in self._svd_file_map if name.lower().startswith(partial_lower)]
        if len(matches) == 1:
            return matches[0]
        elif matches:
            # 优先选择非解锁版本
            normal = [n for n in matches if "Unlock" not in n and "Factory" not in n]
            if normal:
                return normal[0]
            return matches[0]

        return None

    def _build_index(self, device_name: str, device: DeviceSVD) -> None:
        """为设备的所有外设和寄存器建立索引.

        Args:
            device_name: 设备名称
            device: 设备 SVD 数据
        """
        # 建立外设索引
        peripheral_idx: Dict[str, PeripheralInfo] = {}
        register_idx: Dict[str, Dict[str, RegisterInfo]] = {}

        for peripheral in device.peripherals:
            peripheral_idx[peripheral.name] = peripheral

            # 建立寄存器索引
            reg_idx: Dict[str, RegisterInfo] = {}
            for register in peripheral.registers:
                reg_idx[register.name] = register
            register_idx[peripheral.name] = reg_idx

        self._peripheral_index[device_name] = peripheral_idx
        self._register_index[device_name] = register_idx

        logger.debug(f"为 {device_name} 建立索引: {len(peripheral_idx)} 外设")

    def _parse_svd_file(self, svd_path: Path) -> Optional[DeviceSVD]:
        """解析单个 SVD 文件."""
        tree = ET.parse(svd_path)
        root = tree.getroot()
        self._expand_inheritance(root)

        # 解析设备基本信息
        device = DeviceSVD(
            name=root.findtext("name", ""),
            vendor=root.findtext("vendor", ""),
            version=root.findtext("version", ""),
            description=root.findtext("description", ""),
            cpu=self._parse_cpu(root.find("cpu")),
            peripherals=[]
        )

        peripherals = root.find("peripherals")
        if peripherals is not None:
            defaults = self._register_properties(root)
            for peripheral in peripherals.findall("peripheral"):
                device.peripherals.append(self._parse_peripheral(peripheral, defaults))

        return device

    @staticmethod
    def _merge_elements(base, override):
        """复制继承内容，再按名称合并寄存器/字段及本地属性。"""
        merged = deepcopy(base)
        merged.attrib.update(override.attrib)
        for child in override:
            name = child.findtext("name")
            existing = next((item for item in merged
                             if item.tag == child.tag and item.findtext("name") == name), None)
            if existing is not None:
                index = list(merged).index(existing)
                merged.remove(existing)
                replacement = (SVDManager._merge_elements(existing, child)
                               if len(child) else deepcopy(child))
                merged.insert(index, replacement)
            else:
                merged.append(deepcopy(child))
        return merged

    @classmethod
    def _expand_inheritance(cls, root) -> None:
        """递归展开外设和寄存器继承，支持前向引用并拒绝循环/缺失引用。"""
        container = root.find("peripherals")
        if container is None:
            return
        originals = {p.findtext("name", ""): p for p in container.findall("peripheral")}
        resolved = {}
        visiting = set()

        def peripheral(name):
            if name in resolved:
                return resolved[name]
            if name in visiting:
                raise ValueError(f"SVD 外设继承循环: {name}")
            if name not in originals:
                raise ValueError(f"SVD 继承外设不存在: {name}")
            visiting.add(name)
            element = originals[name]
            parent = element.get("derivedFrom")
            result = cls._merge_elements(peripheral(parent), element) if parent else deepcopy(element)
            result.attrib.pop("derivedFrom", None)
            visiting.remove(name)
            resolved[name] = result
            return result

        for name in originals:
            peripheral(name)

        registers = {}
        for name, element in resolved.items():
            for register in element.findall("registers/register"):
                registers[(name, register.findtext("name", ""))] = register
        resolved_registers = {}
        visiting_registers = set()

        def register(key):
            if key in resolved_registers:
                return resolved_registers[key]
            if key in visiting_registers:
                raise ValueError(f"SVD 寄存器继承循环: {'.'.join(key)}")
            if key not in registers:
                raise ValueError(f"SVD 继承寄存器不存在: {'.'.join(key)}")
            visiting_registers.add(key)
            element = registers[key]
            parent = element.get("derivedFrom")
            if parent:
                parent_key = tuple(parent.split(".", 1)) if "." in parent else (key[0], parent)
                result = cls._merge_elements(register(parent_key), element)
            else:
                result = deepcopy(element)
            result.attrib.pop("derivedFrom", None)
            defaults = cls._register_properties(resolved[key[0]], cls._register_properties(root))
            for tag, value in defaults.items():
                if result.find(tag) is None:
                    ET.SubElement(result, tag).text = value
            visiting_registers.remove(key)
            resolved_registers[key] = result
            return result

        for name in originals:
            element = resolved[name]
            register_container = element.find("registers")
            if register_container is not None:
                for old in list(register_container):
                    if old.tag == "register":
                        index = list(register_container).index(old)
                        register_container.remove(old)
                        register_container.insert(index, register((name, old.findtext("name", ""))))
        for old in list(container):
            if old.tag == "peripheral":
                index = list(container).index(old)
                container.remove(old)
                container.insert(index, resolved[old.findtext("name", "")])

    @staticmethod
    def _register_properties(element, inherited=None):
        properties = dict(inherited or {})
        for tag in ("size", "access", "resetValue"):
            value = element.findtext(tag)
            if value is not None:
                properties[tag] = value
        return properties

    def _parse_cpu(self, cpu_element) -> CPUInfo:
        """解析 CPU 信息."""
        if cpu_element is None:
            return CPUInfo(name="Unknown")

        return CPUInfo(
            name=cpu_element.findtext("name", ""),
            revision=cpu_element.findtext("revision"),
            endian=cpu_element.findtext("endian"),
            mpu_present=cpu_element.findtext("mpuPresent", "false") == "true",
            fpu_present=cpu_element.findtext("fpuPresent", "false") == "true",
            nvic_prio_bits=self._parse_int(cpu_element.findtext("nvicPrioBits", "0"))
        )

    def _parse_peripheral(self, peripheral, inherited=None) -> PeripheralInfo:
        """解析外设信息."""
        defaults = self._register_properties(peripheral, inherited)
        registers_element = peripheral.find("registers")
        registers = []
        if registers_element is not None:
            for register in registers_element.findall("register"):
                registers.append(self._parse_register(register, defaults))

        return PeripheralInfo(
            name=peripheral.findtext("name", ""),
            description=peripheral.findtext("description"),
            group_name=peripheral.findtext("groupName"),
            base_address=self._parse_int(peripheral.findtext("baseAddress", "0")),
            registers=registers
        )

    def _parse_register(self, register, inherited=None) -> RegisterInfo:
        """解析寄存器信息."""
        defaults = self._register_properties(register, inherited)
        fields_element = register.find("fields")
        fields = []
        if fields_element is not None:
            for field in fields_element.findall("field"):
                parsed_field = self._parse_field(field)
                if parsed_field.access is None:
                    parsed_field.access = defaults.get("access")
                fields.append(parsed_field)

        return RegisterInfo(
            name=register.findtext("name", ""),
            description=register.findtext("description"),
            address_offset=self._parse_int(register.findtext("addressOffset", "0")),
            size=self._parse_int(defaults.get("size", "32")),
            access=defaults.get("access"),
            reset_value=self._parse_int(defaults.get("resetValue")),
            fields=fields
        )

    def _parse_field(self, field) -> FieldInfo:
        """解析字段信息（包含预计算的 bit_mask 和 enum_map）."""
        bit_width = self._parse_int(field.findtext("bitWidth", "1")) or 1
        bit_mask = (1 << bit_width) - 1  # 预计算 mask

        # 解析枚举值并预计算 enum_map
        enum_element = field.find("enumeratedValues")
        enumerated_values = []
        enum_map: Dict[int, Tuple[str, Optional[str]]] = {}
        
        if enum_element is not None:
            for enum_value in enum_element.findall("enumeratedValue"):
                val = self._parse_int(enum_value.findtext("value", "0"))
                if val is not None:
                    name = enum_value.findtext("name", "")
                    desc = enum_value.findtext("description")
                    enumerated_values.append(EnumeratedValue(
                        name=name,
                        value=val,
                        description=desc
                    ))
                    # 预计算 enum_map 用于 O(1) 查找
                    enum_map[val] = (name, desc)

        return FieldInfo(
            name=field.findtext("name", ""),
            description=field.findtext("description"),
            bit_offset=self._parse_int(field.findtext("bitOffset", "0")) or 0,
            bit_width=bit_width,
            bit_mask=bit_mask,  # 预计算的 mask
            access=field.findtext("access"),
            reset_value=self._parse_int(field.findtext("resetValue")),
            enumerated_values=enumerated_values,
            enum_map=enum_map  # 预计算的枚举字典
        )

    @staticmethod
    def _parse_int(value: Optional[str]) -> Optional[int]:
        """解析整数值（支持十进制和十六进制）."""
        if value is None:
            return None
        value = value.strip()
        if value.startswith("0x") or value.startswith("0X"):
            return int(value, 16)
        return int(value)

    # ==================== 查询接口 ====================

    @_synchronized
    def is_available(self) -> bool:
        """检查 SVD 是否可用."""
        return len(self._svd_file_map) > 0

    @property
    @_synchronized
    def device_names(self) -> List[str]:
        """获取所有设备名称."""
        return list(self._svd_file_map.keys())

    @_synchronized
    def get_device(self, device_name: str) -> Optional[DeviceSVD]:
        """获取指定设备的 SVD 信息."""
        if self._ensure_device_loaded(device_name):
            # 返回实际加载的设备名
            actual_name = self._find_matching_device(device_name) or device_name
            return self._devices.get(actual_name)
        return None

    @lru_cache(maxsize=32)
    def _get_peripherals_cached(self, device_name: str) -> Tuple[PeripheralInfo, ...]:
        """获取指定设备的所有外设（内部缓存方法）.

        Returns:
            外设元组（不可变，支持缓存）
        """
        if self._ensure_device_loaded(device_name):
            actual_name = self._find_matching_device(device_name) or device_name
            device = self._devices.get(actual_name)
            if device:
                return tuple(device.peripherals)
        return ()

    @_synchronized
    def get_peripherals(self, device_name: str) -> List[PeripheralInfo]:
        """获取指定设备的所有外设.

        Returns:
            外设列表
        """
        return list(self._get_peripherals_cached(device_name))

    @_synchronized
    def get_peripheral(self, device_name: str, peripheral_name: str) -> Optional[PeripheralInfo]:
        """获取指定外设（O(1) 索引查找，索引不存在时回退到线性查找）."""
        if self._ensure_device_loaded(device_name):
            actual_name = self._find_matching_device(device_name) or device_name
            
            # 优先使用索引
            peripheral_idx = self._peripheral_index.get(actual_name, {})
            if peripheral_idx:
                return peripheral_idx.get(peripheral_name)
            
            # 索引不存在时回退到线性查找（兼容直接设置 _devices 的情况）
            device = self._devices.get(actual_name)
            if device:
                for peripheral in device.peripherals:
                    if peripheral.name == peripheral_name:
                        return peripheral
        return None

    @_synchronized
    def get_register(self, device_name: str, peripheral_name: str, register_name: str) -> Optional[RegisterInfo]:
        """获取指定寄存器（O(1) 索引查找，索引不存在时回退到线性查找）."""
        if self._ensure_device_loaded(device_name):
            actual_name = self._find_matching_device(device_name) or device_name
            
            # 优先使用索引
            register_idx = self._register_index.get(actual_name, {})
            peripheral_regs = register_idx.get(peripheral_name, {})
            if peripheral_regs:
                return peripheral_regs.get(register_name)
            
            # 索引不存在时回退到线性查找（兼容直接设置 _devices 的情况）
            peripheral = self.get_peripheral(device_name, peripheral_name)
            if peripheral:
                for register in peripheral.registers:
                    if register.name == register_name:
                        return register
        return None

    @_synchronized
    def parse_register_value(
        self,
        device_name: str,
        peripheral_name: str,
        register_name: str,
        value: int
    ) -> Optional[Dict[str, Any]]:
        """解析寄存器值，返回各字段的解释（使用预计算的 mask 和 enum_map，O(1) 枚举查找）."""
        register = self.get_register(device_name, peripheral_name, register_name)
        if not register:
            return None

        field_results = []
        for field in register.fields:
            # 使用预计算的 mask，如果为 0 则动态计算（向后兼容）
            bit_mask = field.bit_mask if field.bit_mask > 0 else ((1 << field.bit_width) - 1)
            field_value = (value >> field.bit_offset) & bit_mask

            # 使用预计算的 enum_map 进行 O(1) 查找
            enum_name = None
            enum_description = None
            
            if field.enum_map:
                # O(1) 字典查找
                enum_name, enum_description = field.enum_map.get(field_value, (None, None))
            else:
                # 回退到线性查找（向后兼容）
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

        return {
            "device_name": device_name,
            "peripheral_name": peripheral_name,
            "register_name": register_name,
            "register_description": register.description,
            "raw_value": value,
            "hex_value": f"0x{value:X}",
            "binary_value": format(value, f'0{register.size}b'),
            "fields": field_results
        }

    @_synchronized
    def find_register_by_address(
        self,
        device_name: str,
        address: int
    ) -> Optional[Tuple[PeripheralInfo, RegisterInfo]]:
        """按绝对地址反查寄存器（外设基地址 + 寄存器偏移）.

        不依赖寄存器名称，遍历所有外设定位绝对地址对应的寄存器。
        用于「手动指定地址」场景：只给地址，反查出外设/寄存器名以便解析字段。

        Args:
            device_name: 设备名称
            address: 寄存器绝对地址

        Returns:
            (外设, 寄存器) 元组，未找到返回 None
        """
        if not self._ensure_device_loaded(device_name):
            return None

        actual_name = self._find_matching_device(device_name) or device_name
        device = self._devices.get(actual_name)
        if not device:
            return None

        for peripheral in device.peripherals:
            for register in peripheral.registers:
                if peripheral.base_address + register.address_offset == address:
                    return peripheral, register
        return None

    @_synchronized
    def clear_cache(self) -> None:
        """清除查询缓存."""
        self._get_peripherals_cached.cache_clear()
        logger.debug("SVD 查询缓存已清除")


# 全局单例
svd_manager = SVDManager()
