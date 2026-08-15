# JLink MCP Server

**AI 与 J-Link 调试器的桥梁 —— 一个 MCP (Model Context Protocol) 服务器**

> 本项目是 [cyj0920/jlink_mcp](https://github.com/cyj0920/jlink_mcp) 的修复分支（fork），
> 重点修复了与 `pylink-square 2.x` 的 API 不兼容问题，使其可稳定运行于 64 位 Python + 新版 SEGGER J-Link。

---

## ✨ 本分支的修复内容

原包 `jlink-mcp 0.1.x` 是基于旧版 pylink API 编写的，而安装的是 `pylink-square 2.0.x`，
导致连接、复位、寄存器、断点、Flash 等一系列功能无法正常工作。本分支针对性地修复了以下问题：

| 模块 | 原代码（旧 API） | 修复后（pylink-square 2.x） |
|------|------------------|------------------------------|
| 连接 | `connect("")` 空串自动检测 / 补丁库未命中即报错 | 芯片名直接穿透给 pylink 原生数据库 + 通用内核名自动检测（Cortex-M4 等） |
| 复位 | `reset(JLinkFlags.RESET_*)` | `reset(ms=0, halt=...)` |
| 运行 | `reset(RESET_DO_NOT_STOP_IF_HALTED)` | `restart()` |
| 暂停 | `halt()` 异步、无等待 | `halt()` + 轮询 `halted()` + `reset(halt=True)` 兜底 |
| 单步 | `step()` | `step(thumb=True)` |
| 断点 | `set_breakpoint()` / `clear_breakpoint()` | `breakpoint_set()` / `breakpoint_find()` + `breakpoint_clear()` |
| 寄存器 | `"R14 (LR)"`（J-Link 实际返回 `"R14"`） | 统一为 `"R14"`，读取前自动暂停 |
| Flash | `flash_download()` / `erase_range()` | `flash()` / `erase()` |
| 设备信息 | `device_id()` / `device_name()`（不存在） | `core_id()` / 连接时保存的设备名 |
| DLL 路径 | 仅搜索 `C:\Program Files\...\JLink_x64.dll` | 支持 `JLINK_LIB_PATH` 环境变量指定任意路径 |

---

## 功能特性

- **连接管理**：枚举设备、连接 / 断开、状态查询、芯片名智能匹配
- **设备信息**：读取目标芯片信息、内核类型、电压
- **内存操作**：读写内存、读写 CPU 寄存器
- **Flash 操作**：擦除、烧录、校验
- **调试控制**：复位、运行 / 暂停、单步、断点
- **RTT**：实时日志读取 / 写入
- **SVD**：外设寄存器解析
- **GDB Server**：启动 / 停止 GDB 调试服务器

---

## 安装

```bash
git clone https://github.com/llm-afk/jlink-mcp.git
cd jlink-mcp
pip install -e .
```

前置条件：已安装 [SEGGER J-Link Software](https://www.segger.com/downloads/jlink/)，
并确保 `JLink_x64.dll` 可被找到。

### 指定 J-Link DLL 路径（可选）

如果 J-Link 软件安装在非默认位置（例如 `D:\Program Files\SEGGER\JLink_V942`），
可通过环境变量 `JLINK_LIB_PATH` 指定 DLL 目录或完整路径：

```powershell
$env:JLINK_LIB_PATH = "D:\Program Files\SEGGER\JLink_V942"
```

或直接指向 DLL 文件：

```powershell
$env:JLINK_LIB_PATH = "D:\Program Files\SEGGER\JLink_V942\JLink_x64.dll"
```

---

## SVD 文件管理

项目内置了 `GD32C10x.svd`（位于 `jlink_mcp/tool/SVD_V1.5.6/`），用于外设寄存器的解析与字段展示。SVD 文件查找规则：

1. 优先使用环境变量 `JLINK_SVD_DIR` 指定的目录；
2. 否则使用包内目录 `jlink_mcp/tool/SVD_V1.5.6/`。

**添加新 MCU 的 SVD**：把 `.svd` 文件放到 `jlink_mcp/tool/SVD_V1.5.6/` 目录下（文件名即设备名，例如 `STM32F407.svd` 对应 `get_svd_peripherals(device_name="STM32F407")`），或设置 `JLINK_SVD_DIR` 指向自定义目录。首次访问时会自动解析并生成 `.svd_cache/` 缓存（已加入 `.gitignore`），缓存可随时删除，会自动重建。

---

## 快速开始

### 1. 直接启动

```bash
python -m jlink_mcp
```

### 2. 作为 MCP 服务器接入 Claude Code / 客户端

在 MCP 客户端配置中添加：

```json
{
  "mcpServers": {
    "jlink": {
      "command": "python",
      "args": ["-m", "jlink_mcp"],
      "env": {
        "JLINK_LIB_PATH": "D:\\Program Files\\SEGGER\\JLink_V942"
      }
    }
  }
}
```

---

## 使用示例

连接一个 GD32 / STM32 目标（`chip_name` 会直接穿透给 pylink 原生数据库，
支持 `GD32C103VB`、`STM32F407VG`、`Cortex-M4` 等）：

```
connect_device(interface="SWD", chip_name="GD32C103VB")
halt_cpu()
read_registers()
read_memory(address=0x08000000, size=256)
run_cpu()
```

---

## 工具列表

- 连接：`list_jlink_devices` `connect_device` `disconnect_device` `get_connection_status` `match_chip_name`
- 设备信息：`get_target_info` `get_target_voltage` `scan_target_devices` `list_device_patches`
- 内存：`read_memory` `write_memory` `read_registers` `write_register`
- Flash：`erase_flash` `program_flash` `verify_flash`
- 调试：`reset_target` `run_cpu` `halt_cpu` `step_instruction` `get_cpu_state` `set_breakpoint` `clear_breakpoint`
- RTT：`rtt_start` `rtt_read` `rtt_write` `rtt_stop` `rtt_get_status`
- SVD：`get_svd_peripherals` `get_svd_registers` `parse_register_value` `read_register_with_fields` `list_svd_devices`
- GDB：`start_gdb_server` `stop_gdb_server` `get_gdb_server_status`
- 辅助：`get_usage_guidance` `list_scenarios` `get_best_practices` `get_forbidden_operations`

---

## 注意事项

- **弹窗**：连接目标后会自动禁用 J-Link 对话框弹窗，擦除 / 烧录时不会再跳出 GUI 窗口。
- **Flash 整片擦除**：`erase_flash()` 擦除的是整颗芯片的 Flash（含 bootloader 与 app），擦除前请确认固件可重新烧录。
- **运行中访问内存 / 寄存器**：目标运行时读取内存或寄存器会自动暂停目标，操作后需调用 `run_cpu()` 恢复。
- **修改源码后**：需重启 MCP server（或重新加载客户端）才会生效。

---

## 许可证

MIT License，版权归原作者 [cyj0920](https://github.com/cyj0920) 所有。
本分支在其基础上做了 pylink-square 2.x 兼容性修复，详见 [LICENSE](LICENSE)。
