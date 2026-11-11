# 验证记录

## 2026-11-11　0.6.3 实板 RTT、调参与固件恢复

GD32C103CB、J-Link SN 174504233、3.3 V、DLL 9.42，SWD 固定并读回 10000 kHz。基线为本地 0.6.2 `ae963fd`。两个版本各自启动独立的 stdio MCP 和原生驱动进程，顺序使用同一块板。用户已关闭功率输出。

### RTT 双向传输

正式 APP 144 没有链接 RTT。测试在独立源码副本中链接官方 SEGGER RTT `74dfff2d`，使用通道 0，上行环形缓冲区 1024 字节，下行 256 字节。前台程序关闭中断并执行测试协议，未启动电机控制环。固件收到 `U` 生成 65,536 个可预测字节，缓冲区满时等待。收到 `D` 后逐字节检查 65,536 个下行字节并回复结果。`P` 测一次写入及一次读取的往返，`E` 原样回传全部 256 种字节。控制块地址 `0x200042C0` 经 ELF 和 SRAM 核对。

下表为完整 MCP 调用的主机端耗时，包含命令触发、文件输出或 MCU 确认。64 KiB 各预热 1 轮、测量 5 轮取中位数。往返预热 1 轮、测量 15 轮。行中的响应字节数按 MCP 文本内容的 UTF-8 字节统计。

| 实测项目 | 0.6.2 | 0.6.3 |
|---|---:|---:|
| 1 字节命令和回显 | 15.82 ms | 10.33 ms |
| 64 KiB 下行，含固件逐字节校验和确认 | 3,984 ms，16.1 KiB/s | 546 ms，117.2 KiB/s |
| 64 KiB 上行，原有文本加十六进制读取 | 451.7 KiB/s | 460.2 KiB/s |
| 64 KiB 上行，Base64 分块 | — | 471.2 KiB/s，100,599 响应字节 |
| 64 KiB 上行写文件 | 20 次读取，311,874 响应字节 | 1 次 capture，846 响应字节，493.4 KiB/s |

0.6.3 还完成 5 次 256 字节全值域二进制回环。单次 `capture` 连续采集 4 MiB，端到端 7.635 秒，536.5 KiB/s。全部 4,194,304 字节与期望序列逐字节一致，SHA256 为 `2b07811057df887086f06a67edc6ebf911de8b6741156e7a2eb1416a4b8b1b2e`，J-Link 的主机溢出计数前后均为 0。此生产端允许等待，正式电流环满载时的采样率和丢包率尚未测量。空流的 20 ms 采集窗口在驱动端实测 20.36 ms，返回空文件及明确的超时状态。

原始计时、文件和驱动状态位于 `.local/audit_20261111/rtt_205239_443987/` 的 `comparison.json` 和 `sustained.json`。临时固件 BIN 为 26,844 字节，SHA256 为 `b5a5b86057c25041aeb7a34aefef5487bd4fd7166736a5b196643624199077c9`。本地保留测试程序 `.local/audit_20261111/rtt_bench.c` 和主机脚本 `rtt_compare.py`、`rtt_sustained.py`。

### 正式固件的观测与调参

从桌面 `c1_driver` 当前工作树复制并核对 117 个编译输入，包含工作树中已有的修改。Keil ARMCC 5.06u7 重新构建 APP 144，0 errors / 7 warnings。BIN 为 38,500 字节，与 AXF 的 Flash 加载段和桌面工程的原始 BIN 完全一致。SHA256 为 `b3f14d78cd5ab3a74e12093ab7cdfe2d420d6bc63f4efcc59591f3d181ec0d8d`。

| 正式 APP 实测项目 | 0.6.2 | 0.6.3 |
|---|---:|---:|
| MotorControl，预热后 11 次读取的中位数 | 3.149 ms，8,077 B | 完整 3.133 ms，精简 2.663 ms、2,505 B |
| 16 次 MotorControl 采样的 JSONL | 143,931 B | 45,173 B，类型布局保存一次 |
| 未变化快照的 diff | 8,151 B | 475 B |
| 暂停时连续两次校验写入 SystickCount，7 对中位数 | 5.662 ms | 3.618 ms，每对省去 38,256 字节只读 Flash 比对 |

每行采样均用原始字节重新解码核对，SystickCount 在每轮写入后恢复原值。原始证据在 `.local/audit_20261111/board_202059_022119/comparison.json`。

### 最终板卡与软件状态

正式、临时和恢复三次烧录均使用 `scripts/flash_c1.py`，每次先双读全片 128 KiB 备份，保留末页尾部，写入后读回。刷回正式 APP 后，启动前全片与临时测试前的备份逐字节相同。启动后再读取全片，正式 BIN 及校准页 115–116 一致。相对临时测试前，EEPROM 页 117 在 `0x0801D438` 追加 8 个零字节，对应 `EE_Init()` 的 `VerifyPagesFullWriteVariable(0U, 0U)` 维护记录。相对第一次烧录前，页 117 共增加 1 个活动页标记和 4 条这种零记录。用户 EEPROM 参数记录均一致。最终默认工程 ELF 比对 matched，DBG_CTL 为 `0x400`，CPU running，探针已释放。全片备份、启动后差异和散列见 `.local/audit_20261111/restored-board.json`。

离线回归 184 项通过。0.6.3 wheel 使用本地依赖构建，包内 33 个 Python 文件和 4 份 SVD 均与工作区逐字节一致。独立目录导入和新启动的 MCP 握手均报告 0.6.3，对外仍为 8 个工具。wheel SHA256 为 `611a77e3fe34e83c5208ab9ab44ead3957a321765c49bbae51566de7aadd21ad`，本地记录在 `.local/audit_20261111/wheel-verification.json`。

## 2026-10-15：0.6.2 union 解析与类型化写入（离线）

在本地 `5fbb2bc`（LTO 全局变量 DWARF 继承修复）基础上增加具名 union 成员路径、整个 union 的原始字节读取，以及现有 write 的 variable 分支。工具数量仍为 8；沿用静态地址/类型缓存，不缓存目标值。union 的缺省成员偏移仅按零处理；明确非零偏移、位域和越界选择均拒绝。写入先完成整个值的类型、范围和长度校验，再提交选定标量或一维标量数组，不补齐 union 尾部或结构体 padding。

完整离线回归 **170 项通过**，包括原有 149 项和新增 21 项 union/类型写入检查。使用实际 ELF/DWARF v4 二进制夹具解析类型，写入由模拟 RAM 后端记录；覆盖零偏移、嵌套成员、union 数组、整个 union 的不透明读取、成员越界、匿名/歧义拒绝、缓存隔离、整数/浮点/布尔/枚举/数组编码、const 父对象与数组元素、atomic、RAM 范围及暂停/匹配保护。错误值不会产生部分写入；校验不符、超时和写入中 CPU 恢复运行不会自动重试。日志 `.local/union_20261015/tests-final.log`。

全新 MCP stdio 服务的 tools/list 返回 8 个工具，全部 JSON Schema 有效，write 包含 VariableWrite。标量、数组和枚举字符串三种请求通过真实客户端、MCP 服务和原生子进程解析，因未打开会话而按预期返回 STALE_SESSION，证明新增参数可以路由且未访问目标。证据 `.local/union_20261015/mcp-tools.json`、`mcp-smoke.json` 和 `mcp-server.log`。

0.6.2 wheel 已离线构建，33 个 Python 文件和 4 份 SVD 与工作区逐字节一致；隔离目录导入确认版本 0.6.2、8 个工具和新增请求模型。包 SHA256 `fefcfee153cd76c22e4b0098c0b374e432b9e95a06cf7fbc67a40f2a6afdcdaa`，证据 `.local/union_20261015/release-verification.json`。测试日志、夹具产物和 wheel 均保存在忽略目录，不随源码提交。

本轮只做离线验证，没有连接、暂停或写入实板。此前引用的 ELF SHA256 `c35c026c709de4a08524f2a93cd3215bfa1780beb47d5d9a391c577cbe54c923` 未用于本次验证，不据此声称该 ELF 的具体 union 路径或硬件行为已通过。类型化写入仍要求已暂停、matched ELF 和配置 RAM；提交后清除匹配证据，下次类型访问前须重新 verify_image。

## 2026-10-12：0.6.1 性能优化与 20 MHz 检查

对外仍为 8 个工具。优化 DWARF 表达式解析器复用和最多 256 项的静态地址/类型计划缓存；目标值逐次读取并继续校验映像和配置范围。8/16/32 位数据转换使用批量字节/小端打包，类型读取和只读映像校验使用不扩大范围的对齐宽度；MMIO/SVD 宽度保持显式。匹配镜像块直接跳过逐字节差异遍历，仍计算完整散列和后续块差异。耗时使用 perf_counter，内部 JSON 使用紧凑分隔符并保持 ASCII 转义。

当前 c1_driver 源码经 Keil ARMCC 5.06 update 7 重编译：APP_VERSION=143，0 errors / 7 warnings。AXF SHA256 `6196970b28e603874acecf97771454cd13b155d4989bca78ab1677806928ce15`；重新导出的 BIN 为 38,408 字节，SHA256 `75ec3fe010adf6619baf897f76a71963b12cbc85fe0863f47c1d2120dfb75509`，与 AXF PT_LOAD 字节一致。只生成构建产物，没有改写该工程源码。

用户确认功率输出已关闭后，基线与优化代码均通过完整实板调试脚本：双读全 Flash 备份、编程当前 BIN、校验 BIN 范围外字节保留（含参数区）、ELF matched、main/servo_loop 硬件断点、BusVoltage 写观察点、两种超时策略及 RAM UDF 异常帧恢复。恢复临时 RAM 和 DBG_CTL=0x00000400，最终当前 APP running，探针释放。证据分别在 `.local/validation/debug_20261012_160950/` 和 `.local/validation/debug_20261012_162723/`。这两次使用 auto，发生在要求 20 MHz 之前。

基准使用同一个上述 AXF、探针 SN 174504233 和 auto 模式（另行实际读回 2000 kHz）。真实 MCP 数据为预热后每项 15 次完整请求的中位数；主机基准为 7 组多次调用的每次耗时中位数。主机项目不含 USB/SWD 时间。

| 项目 | 优化前 ms | 优化后 ms | 范围 |
|---|---:|---:|---|
| MotorControl 结构体读取 | 13.8386 | 5.1999 | 真实 MCP，约 2.66 倍 |
| MotorControl.Iq 读取 | 2.6038 | 2.3673 | 真实 MCP |
| 64 KiB Flash，8 位读取 | 418.6373 | 410.8791 | 真实 MCP，约 1.9% 改善 |
| ELF 只读映像校验 | 247.0680 | 239.9328 | 真实 MCP |
| MotorControl 地址/类型解析 | 3.4302 | 0.2001 | 主机，缓存预热后 |
| 64 KiB，8 位字节转换 | 7.4592 | 0.1879 | 主机 |
| 匹配镜像块比较 | 1.8133 | 0.2174 | 主机 |

较大的 Flash 读取仍主要受传输时间影响，主机转换加速不能等同于 SWD 吞吐加速。报告 `.local/performance_20261012/before.json`、`after.json`、`comparison.json` 含平台、固件散列、源码散列和原始耗时，保留对应 pstats 和服务日志。

按用户要求尝试 `speed_khz=20000`。探针 speed_info 报告 BaseFreq=1600000000 Hz、MinDiv=32，可设置 20000 kHz；读回也确为 20000。但固定 20 MHz 的真实 MCP 连接两次失败。独立 PyLink 诊断先以 auto 读取 CPUID=0x410FC241、DBG_ID=0x17120410，固件全部 38,408 字节与 BIN 相同；随后设为 20000 kHz，第一次 CPUID 读取即返回 Unspecified error。提前设定速度再选择设备也未解决。上述结果没有形成 20 MHz 的性能或稳定性通过证据。

20 MHz 下厂商 InitTarget 日志报告 secured/unsecure 拒绝，auto 下报告 No Protection is set；不能据此认定芯片真正受保护，20 MHz 诊断没有解锁、擦除或重新烧录。失败时释放了探针。失败证据在 `.local/performance_20261012/after_20m_fixed.json` 及服务日志，独立诊断在 `probe_speed_diagnostic.json`。若要使用 20 MHz，仍需检查目标支持的接口速率与信号质量后重测；[SEGGER 排障说明](https://kb.segger.com/J-Link_Troubleshooting)也要求用原生工具确认接口设置和信号质量。

用户随后同意使用 10 MHz 或探测其他有效速率。固定 10000 kHz 的真实 MCP 全部只读基准通过，逐请求和最后状态均读回 10000 kHz，CPU 始终 running，ELF matched。每项仍为 15 次请求中位数，报告 `.local/performance_20261012/after_10m.json`：

| 项目 | 基线 auto ms | 优化后 10 MHz ms | 总加速倍数 |
|---|---:|---:|---:|
| 64 KiB Flash，8 位读取 | 418.6373 | 108.2371 | 3.87 |
| MotorControl 结构体读取 | 13.8386 | 3.3794 | 4.10 |
| ELF 只读映像校验 | 247.0680 | 63.8599 | 3.87 |

此表包含软件优化和速率变化两个因素，不将全部收益归因于代码。c1_driver 最终配置固定为 10000 kHz；这一次是用户允许后的显式选择。完整实板脚本再于 10 MHz 通过上述备份、烧录、保留范围、断点/观察点、超时和真实 UDF 故障恢复流程，证据 `.local/validation/debug_20261012_164849/`。最终 BIN/ELF 校验通过、CPU running、DBG_CTL 与临时 RAM 恢复、探针释放。最终只读复验也通过，证据 `.local/validation/profile_20261012_165103/`，包含 8 工具 schema、8/16/32 位一致性、类型读取、快照/差异/采样、配置检查和连接释放。

随后重新连接出现故障：10 MHz 再次连接失败，8 MHz 也失败。独立原生诊断依次测试 10/8/6/4/2 MHz，都在连接阶段失败；有/无探针能力查询的 10 MHz 试验均失败，排除了本次新增查询是唯一触发条件。参考电压保持 3300 mV，日志报告无法清除 sticky 错误/无法给调试端口上电。6 MHz 的探针量化读回为 5993 kHz，其余测试整数档位读回为请求值；这些均没有形成成功的读取证据。

100 kHz 连接及一次 30 ms 复位脉冲后的重连仍失败，复位引脚读回曾从 1→0→1，最后释放为高；没有解锁、擦除或 Flash 写入，探针已释放。证据 `.local/performance_20261012/native_speed_reconnect.json`、`recover_connection.json`、`after_8m.json` 和 `readonly-release.log`。因此不能将后续故障只归因于高速率，也不能声称已经找到接口最高速率。

用户随后确认 MCU/J-Link 已重新上电。最终源码在 10000 kHz 下再次通过全部 15 次/项的真实 MCP 基准，报告 `.local/performance_20261012/after_10m_repower.json`；64 KiB 8 位读取中位数 108.4223 ms，MotorControl 为 3.3357 ms，映像校验为 63.9352 ms，与前一次成功结果接近。完整固件映像 matched，前后 CPU running。再通过 6 次连续关闭/打开会话，每次比较 64 KiB Flash 的 8/16/32 位读取数据完全一致并验证映像 matched；第 6 次会话持续 60 轮、约 1 秒间隔读取 CPUID、固定 Flash 和实时 MotorControl.Iq，速率读回始终 10000 kHz、CPU 始终 running，最后释放探针。证据 `.local/performance_20261012/audit_reconnect.json`。当前配置保留明确验证通过的 10000 kHz，不自动降速或重复未知结果的写入。重新上电恢复的具体根因未确定，不将它解释为已解决的固件缺陷。

固定速度由 session/profile 显式选择，核对硬件能力和实际读回；操作前后读回不符时失败、使匹配证据失效，不在软件中自动降速。晚于写入的速率异常保留未知完成状态且不重试。完整离线回归 **140 项通过**，含字节序/高位值、缓存隔离/容量、匹配差异和读数新鲜性，以及速率透传、配置继承/覆盖、不符读回、失败释放、禁止自动重试等检查。最终日志 `.local/performance_20261012/tests-release.log`。

只读复现：`scripts/benchmark.py --elf <当前AXF> --serial 174504233 --speed-khz 10000 --output <新JSON路径>`；不带 serial 时只运行主机基准，不触碰硬件，失败也保存报告。软件测试与 10 MHz 实板通过、20 MHz 实板失败分开记录。

最终安装包 `.local/performance_20261012/dist/jlink_mcp-0.6.1-py3-none-any.whl` 已离线构建；33 个 Python 文件与工作区逐字节一致，包含 4 份 SVD，解压后的隔离导入确认为版本 0.6.1 和 8 个工具。SHA256 `0d508917db733a982806fdedde0c09b528c871c3bb475a0da0c3205eb37855a6`，报告 `.local/performance_20261012/release-verification.json`。固件、Flash 备份、安装包和原始测试数据保存在忽略的 .local 目录，不随源码上传。

## 2026-10-12：0.6.0 接口收敛与实板复验

基于 GitHub main 的 `755a892` 收敛到 8 个工具：capture 的 snapshot/diff/sample 归入 read，breakpoint 的设置/移除/列举归入 control。read 明确 action="read"，断点动作明确 breakpoint_set/remove/list；原有能力、类型边界、会话锁、硬件断点归属和超时策略保留。原生请求模型共用登记表并在子进程启动时缓存解析器。

中文 Windows 首次连接触发 GBK/UTF-8 不一致，返回 BACKEND_LOST。内部 JSON 响应改用 ASCII 转义，回归覆盖 cp936 子进程输出中文和 emoji，能够还原完整 Unicode 数据。

- 最终离线回归 **120 项通过**，包含原有 114 项、Unicode 传输回归和 5 项收敛后契约/路由保护检查。日志：`.local/validation/20261012/final-tests.log`。
- 真实 MCP stdio 的 tools/list 返回 8 个工具；JSON Schema 检查通过。按相同 JSON 序列化方式，工具定义从 28,621 字节减到 26,005 字节，减少 9.14%；这是字节体积比较，不是 token 计数。
- 实板配置 GD32C103CB、SWD、J-Link SN 174504233，目标电压 3.3V。新启动服务验证了 8/16/32 位读取一致、工程 preflight、ELF 校验、默认类型读取保护、显式调查模式解码、read snapshot/diff/sample、control breakpoint_list/wait 及连接释放。
- 证据目录：`.local/validation/profile_20261012_154612/`，含请求/响应、8 工具 schema、源码哈希、采样与日志。CPU 前后 running；DBG_CTL 前后均为 0x00000400；连接已释放。
- 0.6.0 wheel 已离线构建，并直接从 wheel 导入确认版本与 8 个工具。33 个 Python 文件与工作区逐字节一致，包含 4 份 SVD；SHA256 为 `e5b6f3a69e700fccade1c6f8506f15d0dcd55919069a267a8d65b23fc986ad9c`。产物及校验结果位于 `.local/validation/20261012/dist/`。

本次存在明确的固件证据限制：当前 AXF SHA256 为 `f3e355038dd85ca0b5a123ea9c438030013bfdf49dcc911a68ea7e1a653a27f3`，板上只读镜像比较 38,164 字节，其中 33,770 字节不同，结果 mismatch。默认变量读取正确返回 IMAGE_NOT_MATCHED；显式 require_match=false 仅用于验证解析与原始数据的对应，不能将这些数值解释为当前固件的可信变量。

本地同名 BIN 为 38,360 字节，AXF 的 PT_LOAD 文件载荷为 38,408 字节，二者也不一致。preflight 显示 FWDGT_HOLD=0；本次未执行暂停、单步、断点设置、擦写或故障注入。对应路径与调用已迁移的完整调试脚本仍要求禁用功率输出并验证 AXF/BIN 一致后才能运行。这次没有重新构建或修改 c1_driver。

以下为历史版本验证，工具数量和当时固件哈希仅适用于对应版本。

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
