# 迁移到精简接口

0.3 将原 44 个 MCP 工具收敛为 10 个；旧名不注册。旧验证脚本和历史材料已归档，不再作为当前项目入口；对外兼容契约为 MCP 工具 schema，私有 Python 模块不保证旧导出兼容。

每个新工具除 discover 外接收 `{"request": {...}}`，目标操作携带 `session_id`。应先读取客户端最新 tools/list，不能继续使用缓存的旧 schema。

| 原工具 | 新工具 |
|---|---|
| list_jlink_devices | discover |
| connect_device | session open；明确芯片、探针序列号 |
| disconnect_device / get_connection_status | session close / status |
| get_target_voltage / get_target_info | session status 的 connection 与会话目标信息；不再单独注册 |
| read_memory / read_registers / read_register_with_fields | read 的 memory / register / peripheral 项 |
| write_memory / write_register / write_register_by_address | write 单项 |
| halt_cpu / run_cpu / reset_target / step_instruction | control halt / resume / reset / step |
| set_breakpoint / clear_breakpoint | breakpoint set / remove；remove 使用返回的 breakpoint_id |
| program_flash / verify_flash | firmware program / verify；原始 BIN 文件，program 强制校验 |
| erase_sector | firmware erase_pages；要求明确页大小且地址对齐 |
| erase_flash | firmware erase_chip；确认目标芯片名，没有范围回退 |
| rtt_* | channel open / read / write / status / close |
| list_svd_devices | discover 的 svd_devices |
| get_svd_peripherals / get_svd_registers | inspect svd |
| parse_register_value | read peripheral 返回 decoded；不再单独注册 |
| start/stop/get_gdb_server_status | 不再暴露；后续 GDB 后端整合前不支持 |
| match_chip_name / scan_target_devices / list_device_patches | 不再注册；使用明确的芯片型号 |
| get_usage_guidance / list_scenarios / get_best_practices / get_forbidden_operations / get_system_prompt | debug://guide 资源与 README |

## 有意改变的行为

- 读 CPU 寄存器不再自动暂停，先 control halt，再 read consistency=halted。
- open 不再默认连接第一个探针，避免多探针时误选。
- 不再接受无效/多余参数；没有实现的操作不会以 success=true 返回空结果。
- 写入没有隐式重试；错误结果不保证硬件没改变。固件修改始终使旧快照失效。
- close 不自动继续 CPU，也不清除其他会话或其他调试器创建的断点。
- 单步/断点要求 architecture=cortex-m。unknown 配置可以读写内存，但不会假定 Thumb 指令集。
- 本地 ELF SHA256 和固件 BIN SHA256 分别表示本地文件及本次操作镜像，二者不可当作目标匹配证明。

## 验证入口

- `scripts/validate_readonly.py --serial 174504233`：只读验证，不暂停/复位/烧录。
- `scripts/validate_debug.py --serial 174504233 --power-stage-off`：完整调试验证，须先禁用功率输出；双读备份 Flash 后使用当前 AXF/BIN，验证源码/观察点/超时/异常栈。成功保留当前 App，失败恢复原备份。

两者均通过独立 MCP stdio 进程，结果输出到 `.local/validation/`。详见 [验证记录](VALIDATION.md)。

驱动超时终止本服务拥有的驱动进程树，保持 MCP 主进程存活。返回 session_lost=true 时必须重新连接并检查目标状态，不自动重试完成未知的写入。

## 0.3 → 0.4 增量更新

工具名保持 10 个，现有请求仍有效。session open 新增 profile_path；read/capture 新增 variable 项；inspect 新增 variable/preflight；firmware 新增 verify_image。刷新客户端 tools/list 后可用。

带类型读取默认要求配置边界和只读映像匹配，原有 symbol 原始字节读取语义不变。verify_image 可以在运行中执行；其他 Flash 操作仍要求显式暂停。配置、支持的 DWARF 范围及匹配证据限制见 README。

只读实板复现：

```powershell
.\.venv\Scripts\python.exe scripts/validate_readonly.py --serial 174504233
```

此脚本不写寄存器、不暂停/复位/烧录。若镜像不匹配，先确认默认类型读取被拒绝，再显式 require_match=false 检查解码链路；不能把此时读数解释为当前固件的可信变量值。

## 0.4 → 0.5 调试能力

仍为 10 个工具。control 新增 wait/run_until，inspect 新增 context，fault 新增异常栈和标志解析；breakpoint 新增 kind=read/write/access，使用 variable 或 address+size。capstone 是新的反汇编依赖。

执行断点统一为硬件断点，不再允许底层自动回退 Flash 软件断点。函数 symbol 和变量观察点要求 verify_image=matched；既有基于明确地址的请求保持有效。resume 可能返回 observed_halted（快速命中），客户端应读取 completion 和 stop，不应假设 success 就代表 CPU 正在运行。

实板脚本 `scripts/validate_debug.py --serial 174504233 --power-stage-off` 使用当前 c1_driver 的 AXF/BIN：先验证二者装载字节一致，双读备份 Flash，再编程、检查范围外保留、验证源码/观察点/超时/真实 UDF 异常。成功时保留当前编译 App 并运行；失败时恢复原备份。测试恢复 RAM 临时指令和 DBG_CTL，代码中不修改驱动源码。实际执行前必须确认功率输出已禁用。

## 0.5.1 目录整理

10 个工具及其请求契约保持不变，依赖统一以 pyproject.toml 为准，删除 requirements.txt 的重复声明和未使用的直接 pydantic-settings 依赖。

移除未注册的旧提示词/设备信息包装模块、旧全局提示词配置和未使用的旧请求模型。当前底层适配器及全部回归测试保留。README 位于根目录，迁移与验证说明集中到 docs，实板脚本集中到 scripts。
