# J-Link MCP 实板验证：2026-09-29

结论：GD32C103CB + SWD 的核心连接、读写、烧录、页擦除、调试和 RTT 双向通信可用；不能判为所有接口均合理或完整可靠。以下缺陷已经通过实板或离线复现确认。本轮仅添加验证材料，没有修改 MCP 产品代码或原 c1_driver 仓库。

## 环境与构建

- MCP 提交：`53d2b131820d01a03d2514a477ba577f14a912f9`，初始工作区干净。
- 探针：用户提供的 J-Link PRO，USB SN `174504233`。枚举接口仅返回通用名称 `J-Link`。
- 目标：显式指定 `GD32C103CB`、SWD；VTref 报告 3.3 V；Cortex-M4；CPUID `0x410FC241`；DBG.ID `0x17120410`。
- Python 3.11，pylink-square 2.0.1、MCP 1.29.0、Pydantic 2.13.4；J-Link API 版本字段报告 9.42。工具的 `firmware_version` 实际取 `jlink.version`，不能据此认定探针固件版本。
- 用户确认仅调试供电，功率级未供电，允许暂停/复位，并后续授权编译/修改测试固件及烧录。
- `C:\Users\32196\Desktop\c1_driver\Firmware_app` 复制到本工作区 `hardware_validation/Firmware_app`。
- c1_driver 提交：`ac76b6b7db1c7c73150fb1a45f780c855ca35b08`；原仓库仅有已有未跟踪 `.claude/`。
- 原应用独立构建：ARMCC 5.06 update 7 build 960，0 错误、7 警告；仅禁用依赖原目录的打包后处理，保留 BIN 转换。警告详见 `baseline_build.log`。
- RTT 测试目标使用 c1_driver 的 startup、system、CMSIS 与 misc 源码，另加本地 SEGGER RTT 库和专用 main；不初始化 PWM、电机或看门狗。最终构建 0 错误、0 警告，BIN 2000 字节，链接基址 `0x08000000`。
- 本轮烧录并运行的是上述专用测试固件；完整 c1_driver 应用仅构建，没有将新构建应用当作实板功能验收结果。

## 实板结果

| 项目 | 结果与证据 |
|---|---|
| 枚举、指定 SN 连接、状态、电压、断开/重连 | 通过 |
| 目标信息 | 名称/内核可用，Flash/RAM 大小返回 null，device_id 实际复用 core_id |
| Flash/SRAM 读取、CPU 寄存器 | 暂停稳定时通过；原应用运行时存在下述状态缺陷 |
| R0 写入与恢复 | `0x12345678` 读回一致，恢复原值 |
| SRAM 与按地址写入 | `0x20000000` 模式读回一致；初次原应用测试恢复原 SRAM，专用固件测试写入专用 scratch |
| SVD | 列出 GD32C10x/N32H473/474/475；DBG 寄存器枚举与 ID 字段读取通过 |
| 复位、暂停、运行、单步 | 专用固件通过，示例单步 PC `0x08000264 → 0x08000266` |
| 断点 | 实际运行命中 `validation_checkpoint` 的 `0x08000778`，清除并继续成功 |
| program_flash 文件与 hex 数据 | 2000 字节固件、3072 字节测试数据及完整 128 KB 恢复镜像均匹配读回校验 |
| verify_flash | 原 Flash 向量前 8 字节匹配 |
| erase_sector | `0x0801F800` 擦除 1024 字节后全 FF；`0x0801F400` 的 A5 页与 `0x0801FC00` 的 5A 页保持一致 |
| RTT 上行 | 实收 `JLINK_MCP_VALIDATION_20260929 boot` 与 `... alive`，71 字节 |
| RTT 下行与回显 | 写入 `MCP_PING\n` 9 字节，收到 `ECHO:MCP_PING\n` 14 字节 |
| RTT 启停 | 测试固件正常；断连状态清理存在下述缺陷 |
| GDB 启动/停止与协议握手 | 本机 2331 端口响应 `qSupported`，返回 PacketSize=10000、QStartNoAckMode+、hwbreak+、features:read+；未做完整 GDB 调试会话 |

首轮 18 字节 RTT 消息只写入 15 字节：这是默认 16 字节下行环形缓冲区的容量限制，接口返回了实际写入长度。首轮测试的“必须一次全部写入”断言不适用，已改用 9 字节回显消息完成验证；上层发送长消息必须处理部分写入。

原应用首次 `halt_cpu` 返回错误 303；随后 `read_registers` 却报告成功并返回 19 个全零寄存器。复位并暂停后 PC=`0x08000168`、SP=`0x20007408`、XPSR=`0x01000000`，读取恢复正常。源码启用了约 200 ms 看门狗，而 pylink 暂停调用存在等待；这与现象一致，但没有单独验证看门狗是唯一原因。

## 已确认的问题，按优先级

1. **P1：地址范围擦除会整片擦除。** `tools/flash.py:75` 在传入 start/end 时仍调用无参数 `jlink.erase()`。离线模拟明确复现；实板没有执行该危险分支。应拒绝不支持的范围，或实现真正受限的范围擦除，不能在执行后才提示整片擦除。
2. **P1：CPU 寄存器读取没有可靠暂停确认。** `tools/memory.py:166` 附近暂停后不核实 halted，失败被吞掉；实板出现全零成功结果。所有寄存器读取异常时也会返回 `success=true, registers=[]`，离线已复现。
3. **P1：width 没有传给底层。** `tools/memory.py:49,121` 及 `tools/svd.py:425,511` 没有传 pylink 的 `nbits`。width=32 仅影响对齐/长度，实际访问宽度由 DLL 自动决定；对有访问宽度要求或副作用的 MMIO 不能保证语义。已核对当前安装 pylink 源码并记录模拟调用参数。
4. **P1：program_flash 校验不匹配仍 success=true。** `tools/flash.py:260` 离线复现；自动化只检查 success 会误判。`_build_verify_result` 对短读仅 zip 比较，还可能出现 matched=false、mismatch_count=0。
5. **P2：RTT 中文跨读取边界丢失。** 最新提交修复了 list/bytes 兼容，相关正常路径测试通过；但 `errors='ignore'` 会直接丢弃半个 UTF-8 字符，latin1 fallback 不会补救。将“中”的 E4 / B8 AD 分两次返回，两次输出均空。另 `timeout_ms/read_mode` 没有实现实际等待/模式控制。
6. **P2：RTT 生命周期与连接脱节。** 实板 RTT start 后 disconnect，连接状态已 false，rtt_get_status 仍 started=true；重连后的模块状态也可能阻止正常重新启动。已停止 RTT 并清理连接。底层支持的 block_address 没有暴露到 MCP rtt_start。
7. **P2：GDB 参数与报告不一致。** gdb_server.py 将 host 仅保存为字段，没有传给子进程；因此报告“监听 127.0.0.1”不是绑定地址证明。device=None 也传空字符串而非当前芯片。启动只检查 0.5 秒后进程存活。本轮指定明确 device 后实板握手成功，不代表这些边界正确。
8. **P2：非法 reset_type 会复位。** debug.py 对未知字符串走普通复位分支，离线 `reset_type='typo'` 仍实际调用 reset 并返回成功，应在硬件操作前拒绝。

其他限制：scan_target_devices 只读取当前 core_id，不能视作完整总线扫描；普通安装包的 SVD 资源打包情况未验证。整片擦除、core-only reset、目标掉电恢复、多个探针并发和长时间 RTT 压力测试未做。

## 恢复与证据

- 每次真正修改 Flash 前都在暂停状态读取完整 128 KB 两次并比较一致，保存文件；测试结束用 MCP program_flash 恢复，再完整读回逐字节比较。
- 最终恢复的是最终测试开始前的备份，SHA256：`9aece789cf8c7b553bb5208db662fd070fb938935715e0125b9550eef4f58e69`。
- 恢复验证后普通复位、CPU running=true、断开连接；无残留测试断点，RTT/GDB 已停止。
- 首份备份 `hardware_validation/original_flash_08000000_128k.bin` 保留。原应用恢复运行后高地址 Flash 发生变化，因此后续测试使用单独时间戳备份，没有覆盖首份镜像。源代码 EEPROM 区为第 117 页开始的 11 页（0x0801D400–0x08020000）；变化地址详见 physical_results.json。逐字节恢复结论指最终复位运行前的校验时点，不能推导原应用之后不再更新参数。
- 首份与最终测试前备份相差 16 字节，范围 `0x0801D5D8–0x0801D5E7`，位于上述 EEPROM 区；应用代码区域读回 hash 一致。
- `hardware_validation/physical_results.json`：最终完整 MCP 测试记录与恢复 SHA；`physical_results_first_run.json`：第一轮与成功恢复记录。
- `hardware_validation/run_mcp_validation.py`：真实 MCP stdio 客户端，测试和恢复均调用本仓库 MCP 工具；不是绕过 MCP 的 pylink 脚本。
- `audit_20260929.py` / `audit_20260929_results.json`：19 项离线观察，包含缺陷复现。断言通过代表成功复现观察，并不代表这些行为设计正确。
- `prepare_hardware_validation.py`：独立 Keil 项目与 RTT 固件生成脚本；运行会刷新验证副本，应先保存副本内的自定义修改。

建议先修正 P1 的行为契约及对应回归测试，再处理 RTT 状态/解码和 GDB 参数。基础硬件链路已获得实板证据；异常路径仍需修复。
