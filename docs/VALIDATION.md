# 验证记录

**历史材料位置：** 以下 0.5 验证目录、固件备份和旧安装包已移入 `.local/workspace-before-cleanup-20260929.zip`；文中的 `hardware_validation/...` 和旧 `dist/...` 是归档内部路径。当前脚本输出到 `.local/validation/`。

0.5 实现约定的前三项优先能力：等待和停止原因、源码/故障现场定位、数据观察点。对外保持 10 个 MCP 工具。类型写入、条件采集、ELF 直接下载和完整 GDB 调用栈未纳入本轮。

## 接口变化

| 现有工具 | 新增能力 |
|---|---|
| control | wait、run_until，明确命中/其他停止/超时；报告原始停止原因与核心寄存器 |
| inspect | context：PC/LR 的函数与源码映射、目标字节 Thumb 反汇编 |
| inspect fault | CFSR/HFSR 具名标志；受 RAM 边界限制的 ARMv7-M 异常栈恢复 |
| breakpoint | execute/read/write/access；明确地址或 DWARF 变量成员；统一硬件断点 |

run_until 要求已经暂停，临时硬件断点在成功、超时和异常路径清理，复用已有断点时不删除用户断点。on_timeout 默认 halt，也可明确选 running。wait 自身不改变执行状态。原生驱动超时仍属于完成未知，不能承诺断点已清理。

函数符号断点和变量观察点要求当前 ELF 只读映像匹配；明确地址仍可用于不匹配调查。执行断点不回退到软件 Flash 修改。观察点宽度为对齐的 1/2/4 字节，不加入任意表达式和值条件。

inspect context 从目标实际字节反汇编，源码映射来自 ELF，并携带匹配状态。停止 PC 可能位于触发访问之后，不将观察点停止行等同于访问语句所在行。

inspect fault 自动恢复只限异常入口；离开入口后需显式提供 frame_address 和 exc_return，不扫描猜测栈地址。支持 MSP/PSP、基本/扩展浮点栈布局、对齐填充；不解码浮点载荷，不声称完整调用栈。堆栈错误、非法地址和无效帧明确报告。

resume 后立即命中断点时现在返回 observed_halted、running_observed=false 和现场，避免把快速命中误报为等待运行超时。

## 产物与范围

使用用户 2026-09-29 19:10 编译的 `C:/Users/32196/Desktop/c1_driver/Firmware_app/MDK-ARM/object/dgm_app_fw.axf` 及同名 BIN；未修改驱动源码或重新编译。

- ELF SHA256：`57d0bab18e5340f7cf56ba6b12a2457e6875d0cc7c1ecab0fd9482b0b61e59dc`。
- BIN SHA256：`b70c3eb7e4f12c20a59280418e27e5200229abfa6a7418908d2130ecd0a051b2`。
- BIN 长度 40,840 字节，等于 ELF 唯一 PT_LOAD 的文件载荷，装载地址 0x08000000。
- 烧录前双读备份完整 128KB Flash，一致 SHA256 为 `c9f354da66796d88855ad7d6a64e1585a0b694b4eb23bfe4a09fa4a654285ed8`。
- 最后一个擦除页的 BIN 以外尾部取自备份；烧录后、App 启动前，完整读回确认所有 BIN 范围以外字节与备份相同，包含 EEPROM 参数区。

## 实板结果

设备：J-Link PRO SN 174504233、GD32C103CB、SWD，用户已确认功率输出禁用。

完整验证目录：`hardware_validation/debug_20260929_193326/`。该目录保存了当前 AXF/BIN 副本、原始双备份、烧录后备份、MCP 请求/响应、工具 schema、服务日志和当时源码哈希。

| 检查 | 结果 |
|---|---|
| ELF 只读区域比对 | 40,596 字节全部相同，matched |
| run_until main | 硬件断点命中；定位 main.c:386；显示实际 Thumb 指令 |
| run_until servo_loop | 硬件断点命中；定位 motor_ctrl.c:815；临时断点清理 |
| BusVoltage 写观察点 | 捕获 MC_high_priority_task 中真实 CPU 写访问；停止 PC 对应 motor_ctrl.c:1433（写入在前一行） |
| run_until 超时 | halt/running 两种策略均通过，临时断点均清理 |
| wait 超时 | 返回 timeout，CPU 保持 running，无隐式暂停 |
| 真实非法指令异常 | 在未分配的 SRAM 0x20007FF0 临时放置 Thumb UDF，明确跳转后停在 HardFault_Handler 入口 |
| 异常栈恢复 | CFSR=0x00010000（UNDEFINSTR）、HFSR=0x40000000（FORCED）；MSP 基本帧恢复，原出错 PC=0x20007FF0 |
| 最终恢复 | RAM 测试位置恢复、DBG_CTL 恢复 0x00000400、当前 BIN 和 ELF 比对通过、CPU running、探针已释放 |

第一次试验尝试从 0x60000000 取指，未在等待窗口内进入预期断点；测试按失败路径恢复并校验原始完整 Flash。随后改用明确的 UDF 指令，完成上表全部验证。没有把第一次超时解释成已捕获异常。

最终代码增加行表索引检查和具名故障标志后，离线回归 **114 项通过**（本轮新增 29 项）。覆盖断点归属/复用/清理失败、停止原因缺失、超时策略、快速命中、变量和对齐校验、源码序列间空洞、反汇编、MSP/PSP/扩展帧及无效异常栈。扩展浮点栈等边界由离线夹具验证，不声称全部经过实板故障注入。

最终只读复验目录：`hardware_validation/profile_20260929_193801/`。最新代码再次通过真实 MCP tools/list=10、默认配置 ELF 匹配、默认类型读取、结构体解码和快照差异检查，CPU 前后 running。该目录还保存最终源码哈希、114 项测试日志和安装包校验结果。

## 安装包与复现

`dist/jlink_mcp-0.5.0-py3-none-any.whl` 已构建，38 个 Python 文件均与工作区逐字节相同，包含 4 份 SVD 和 Capstone 依赖声明。SHA256：`5a29c5d7f37d330e6ac295d8e51d8ef6d516135bac701fc7944616b892acdc2a`。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/validate_readonly.py --serial 174504233
```

完整侵入性复现脚本为 `scripts/validate_debug.py --serial 174504233 --power-stage-off`。会备份、烧录当前 App 并注入临时 UDF；成功保留当前编译 App，失败恢复备份。只读检查无需运行完整脚本。

设计依据：[SEGGER 调试与观察点命令](https://kb.segger.com/J-Link_Commander)、[Arm Cortex-M4 用户指南](https://www.keil.com/dd/docs/datashts/arm/cortex_m4/r0p1/dui0553a_cortex_m4_dgug.pdf)、[Keil 故障分析说明](https://www.keil.com/appnotes/files/apnt209.pdf)、[Capstone Python API](https://www.capstone-engine.org/lang_python.html)。

## 0.5.1 整理与复验

删除了旧提示词配置、未注册包装模块和未使用请求模型；工具数量、当前能力和全部 114 项测试保留。依赖只在 pyproject.toml 声明。实板脚本迁至 scripts，历史报告收敛为本文件和 MIGRATION.md。

清理前完整快照包含 549 个文件，覆盖复制的 Firmware_app、所有实板备份、旧安装包和被删除源文件；逐文件 SHA256 校验通过。快照 `.local/workspace-before-cleanup-20260929.zip` 的 SHA256 为 `8f16f45534a6c6d6eb379ec570b0120bc2e9fb9a7a2c6a4d2bd3b05bd1c263c0`，内部 MANIFEST.json 列出文件校验值。只清理工作区副本，恢复材料保留在快照中。

整理后 114 项离线测试通过，日志保存在 `.local/cleanup-tests.log`。本次不重复烧录或故障注入，仅进行只读实板复验。

只读复验通过，证据位于 `.local/validation/profile_20260929_195058/`：工具数为 10，ELF matched，变量/结构体与快照差异读取成功，CPU 前后均为 running，连接已释放。

整理后安装包为 `.local/dist/jlink_mcp-0.5.1-py3-none-any.whl`，包含 33 个 Python 文件和 4 份 SVD；所有源码与工作区逐字节一致，已确认删除模块未混入安装包。解压后从隔离路径导入，版本 0.5.1、工具数 10。安装包 SHA256：`1c32cc1c4b47e885dcf649894ee3a88af48d5d917b0c08f69d31feb466c0b0a3`。
