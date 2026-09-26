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
| 暂停 | 暂停失败后复位目标 | `halt()` + 轮询 `halted()`；失败明确报错，保留现场 |
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

项目内置了 `GD32C10x.svd` 以及 `N32H473.svd` / `N32H474.svd` / `N32H475.svd`（均位于 `jlink_mcp/tool/SVD_V1.5.6/`），用于外设寄存器的解析与字段展示。SVD 文件查找规则：

1. 优先使用环境变量 `JLINK_SVD_DIR` 指定的目录；
2. 否则使用包内目录 `jlink_mcp/tool/SVD_V1.5.6/`。

**添加新 MCU 的 SVD**：把 `.svd` 文件放到 `jlink_mcp/tool/SVD_V1.5.6/` 目录下（文件名即设备名，例如 `STM32F407.svd` 对应 `get_svd_peripherals(device_name="STM32F407")`），或设置 `JLINK_SVD_DIR` 指向自定义目录。wheel 安装包也包含内置的 4 个 SVD 文件。

首次查询时解析 SVD，并在用户缓存目录保存 JSON：Windows 为 `%LOCALAPPDATA%/jlink-mcp/svd`，其他系统为 `$XDG_CACHE_HOME/jlink-mcp/svd`（未设置则用 `~/.cache/jlink-mcp/svd`）。缓存按源文件路径和内容校验；缓存损坏或不可写时自动回退到解析源文件，无需修改包安装目录权限。旧 `.svd_cache/` 中的 pickle 文件不再使用。

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
- Flash：`erase_flash` `erase_sector` `program_flash` `verify_flash`
- 调试：`reset_target` `run_cpu` `halt_cpu` `step_instruction` `get_cpu_state` `set_breakpoint` `clear_breakpoint`
- RTT：`rtt_start` `rtt_read` `rtt_write` `rtt_stop` `rtt_get_status`
- SVD：`get_svd_peripherals` `get_svd_registers` `parse_register_value` `read_register_with_fields` `read_register_by_address` `write_register_by_address` `list_svd_devices`
- GDB：`start_gdb_server` `stop_gdb_server` `get_gdb_server_status`
- 辅助：`get_usage_guidance` `list_scenarios` `get_best_practices` `get_forbidden_operations` `get_system_prompt`

---

## 注意事项

- **弹窗**：连接目标后会自动禁用 J-Link 对话框弹窗，擦除 / 烧录时不会再跳出 GUI 窗口。
- **Flash 整片擦除**：必须显式调用 `erase_flash(chip_erase=True)`，会清空 bootloader 与 app。省略该参数或传入 `start_address` / `end_address` 会直接报错，不执行擦除。按页擦除请使用 `erase_sector(address, count, page_size)`，并根据具体芯片设置页大小；实际擦除粒度由 J-Link 的设备算法决定。
- **地址与数据宽度**：MCP 地址参数支持整数和十六进制字符串，例如 `"0x08000000"`。`read_memory` 的 `size` 仍按字节计数，地址及数据长度需按 `width / 8` 对齐；任意字节长度使用 `width=8`。内存数据以小端转换，写入数据使用十六进制字符串。CPU 寄存器支持 `PC`、`SP`、`LR` 别名。
- **完整性检查**：Flash 校验分块读取；短读、短写、烧录后校验不匹配都会明确返回失败。单独调用 `verify_flash` 时，应同时检查 `success`（是否完成校验）和 `matched`（是否一致）。
- **运行中访问内存 / 寄存器**：目标运行时读取内存或寄存器会自动暂停目标，操作后需调用 `run_cpu()` 恢复。
- **长操作与取消**：硬件工具在同一工作线程按顺序执行，MCP 通信和独立 SVD 查询可继续响应。取消尚未执行的请求会移除该操作；已经进入 J-Link DLL 的调用无法强制中断，会完成后再执行下一项。退出时按顺序清理 GDB 与探针连接。
- **RTT 会话**：断开连接会清理 RTT 状态，重新连接后需重新 `rtt_start()`。读取与写入省略 `buffer_index` 时使用启动配置；`continuous` 模式在无数据时最多等待 `timeout_ms`，`once` 模式只读一次。UTF-8 分块字符会保留到后续读取，`data_hex` 始终提供本次原始字节。
- **修改源码后**：需重启 MCP server（或重新加载客户端）才会生效。

### GDB 与 MCP 连接交接

GDB 和 MCP 不能同时占用同一个探针。已有 MCP 连接时显式调用：

```text
start_gdb_server(transfer_connection=True)
```

这会停止 RTT、关闭 MCP 的探针连接，并继承设备、序列号和接口。GDB 运行期间 MCP 硬件访问会被阻止。调用 `stop_gdb_server()` 后，需要显式 `connect_device(...)` 重新连接。

尚未连接 MCP 时，可以直接 `start_gdb_server(device="STM32F407VG")`。默认只允许本机连接（`host="127.0.0.1"`）；需要远程调试时显式使用 `host="0.0.0.0"`。其他 host 值会被拒绝。服务会持续读取进程输出，并等待就绪提示后才返回启动成功。

## 常见问题（FAQ）

**Q：连接时报「找不到 J-Link DLL」？**
本工具会自动扫描常见安装路径（含 C / D / E 盘的 `SEGGER` 目录及 `JLink_Vxxx` 版本目录）。若仍找不到，设置 `JLINK_LIB_PATH` 指向 DLL 目录即可（见上文「指定 J-Link DLL 路径」）。

**Q：`connect_device` 报「无法连接到芯片」？**
检查：① 目标板已上电；② 接口类型正确（多数 Cortex-M 用 SWD）；③ `chip_name` 拼写正确，或用通用内核名 `Cortex-M4` 自动检测。

**Q：读内存 / 寄存器报「目标正在运行」？**
先调用 `halt_cpu()` 暂停目标；工具在读取时通常会自动暂停，读完后按需 `run_cpu()`。

**Q：`erase_flash` 会把整颗芯片擦掉吗？**
显式传入 `chip_erase=True` 才会执行整片擦除，bootloader 与 app 一并清空。范围参数会被拒绝，不会再静默变成整片擦除。若只想擦指定扇区（页），用 `erase_sector(address, count, page_size)`，并核对芯片的实际擦除粒度。

**Q：改了源码 / SVD 后不生效？**
重启 MCP server（或重新加载 MCP 客户端）才会加载新代码。

---

## 开发与离线验证

```bash
python -m pip install -e . setuptools wheel
python -m unittest discover -s tests -v
```

测试使用模拟探针和子进程，不需要 J-Link DLL 或目标板，覆盖错误路径、串行调度及取消、RTT/GDB 生命周期、真实 MCP stdio 通信，以及 wheel 在源码目录之外加载全部内置 SVD。GitHub Actions 配置了 Windows / Linux、Python 3.10 / 3.12 矩阵。真实芯片的烧录、复位与 GDB 行为仍需在对应硬件上验证。

依赖限制在 MCP 1.x（项目使用 FastMCP）、Pydantic 2.x、PyLink 2.x，避免新主版本直接破坏启动兼容性。

## 许可证

MIT License，版权归原作者 [cyj0920](https://github.com/cyj0920) 所有。
本分支在其基础上做了 pylink-square 2.x 兼容性修复，详见 [LICENSE](LICENSE)。
