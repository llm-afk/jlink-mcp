# J-Link MCP 修复与收敛结果

## 重新加载后的再次验证

用户重新添加 MCP 并要求复测后，已在当前 Codex 会话确认新版 rtt_start.block_address、rtt_write.timeout_ms 及 read_memory 新行为说明可见。直接调用当前会话工具：连接成功、Flash 读取前后 CPU 均 running=true、DBG.ID 字段读取正常、断开后 RTT started=false。

26 项离线回归再次全部通过。随后完整实板回归脚本退出码 0，烧录/校验、页擦除及相邻页保护、RTT 66 字节双向消息、中文每次 1 字节读取、断连重连、三种访问宽度、单步/断点命中以及 GDB 默认配置握手均再次通过。结束恢复本次测试前完整 128 KB Flash 并逐字节比较一致，CPU 恢复运行，RTT/GDB 已停止、探针连接已释放。

本轮结果与源码哈希：`hardware_validation/reload_retest_summary.json`；完整工具记录：`hardware_validation/regression_results.json`；离线结果：`hardware_validation/reload_unit_tests.log`。上一轮工具记录已存入 `hardware_validation/before_reload_retest_*`，后文原修复阶段的 SHA 为历史值，本次恢复 SHA 以 reload_retest_summary.json 为准。

基于 `53d2b13` 的修复与验证记录。原始问题证据见 `TEST_REPORT_20260929.md`，该报告描述的是修复前版本。原始硬件日志、Flash 备份和构建副本仅保留在测试机器上，不随仓库发布；仓库包含复现脚本和结果摘要。

## 已完成

- **Flash**：范围请求不再退化为整片擦除；只有显式 `chip_erase=True` 且未传范围才执行。烧录/校验失败返回 `success=false`；短读计入缺失数据，页擦除不再把空读当作全 FF。拒绝 data/file_path 同时输入及直接作为原始数据烧录的 HEX/ELF/AXF 文件。
- **目标访问**：统一字节长度、大小端打包、8/16/32 位实际访问宽度和短读/短写检查。内存与 SVD 读取不自动暂停 CPU；需要一致快照时由调用者显式暂停。CPU 寄存器读取确认暂停，返回逐项错误，不再吞错或全零假成功；支持 PC/SP/LR 别名，默认寄存器名称保持兼容。
- **调试**：非法 reset_type 在操作前拒绝；core reset 不会回退普通复位，并恢复之前的 reset strategy。单步要求目标已经暂停。halt 失败不自动复位。
- **RTT**：按连接清理会话，disconnect/reset/flash 后失效；重复 start/stop 可安全调用。支持显式控制块地址、默认沿用已配置通道、0–30000 ms 有界等待、UTF-8 增量解码及原始 data_hex。写入自动只重试尚未发送的后缀，超时明确报告实际发送字节数。
- **GDB**：默认本机、沿用当前芯片，明确传 LocalhostOnly；不主动 halt/初始化 CPU 寄存器。持续排空输出防止管道堵塞，等待实际监听就绪，失败清理进程。仅接受 localhost/127.0.0.1/0.0.0.0，不假装支持任意绑定地址。
- **文档与安装**：README、MCP schema 说明及内置指导同步，移除“先整片擦除再烧录”等误导性默认流程；wheel 显式打包四个 SVD。

GDB 参数按 [SEGGER 官方文档](https://kb.segger.com/J-Link_GDB_Server) 配置。

## 验证

- `python -m unittest discover -s tests -v`：26 项离线回归通过，覆盖危险输入不得触及硬件、短读/短写、暂停失败、部分寄存器错误、RTT 分包与部分发送、断连清理、GDB 启动失败与资源释放。
- 隔离构建 wheel 成功，确认包含四个 `.svd` 与 `target_access.py`。系统 Python 缺 wheel 时先前非隔离构建失败，最终使用 pip 临时隔离构建成功，没有升级全局依赖。
- 使用全新 MCP stdio 进程加载工作区修复代码，GD32C103CB + SWD + SN 174504233 实板回归成功：
  - 测试固件烧录校验；1 KB 页擦除且前后相邻页不变。
  - RTT 66 字节消息跨默认 15 字节可用下行缓冲区完整发送并回显。
  - 中文回显按每次 1 字节读取，最终完整还原“中文”，未丢失跨次字符。
  - 断连后 RTT started=false，重连和显式控制块地址启动成功，重复 stop 成功。
  - width=8/16/32 三种读写均读回相同字节模式；CPU 暂停/单步/断点实际命中成功。
  - GDB 省略 device 使用当前芯片、本机 2331 端口返回 qSupported 能力响应。
- 复测前完整 Flash 备份两次一致；结束恢复 128 KB，完整读回逐字节一致：SHA256 `8b0c3717698ee4f15b63bf384eaf3ff81429c17f5ceb043f5b4fc90b6beefd40`。之后 CPU 恢复运行并断开 J-Link。

证据：`hardware_validation/regression_results.json`、`hardware_validation/unit_tests.log`；复现脚本：`hardware_validation/run_mcp_regression.py`。原固件运行后可能继续更新 EEPROM，完整镜像一致结论对应恢复后、最终运行前的时点。原 c1_driver 源码未改动。

## 使用与边界

当前已启动的 MCP 服务可能仍缓存旧 Python 模块。重新加载服务或新开会话后，才会使用这些修复；本轮实板验证使用的是新进程。

没有把“看门狗不断复位时无法暂停”伪装成可自动修复：此时返回明确错误，用户允许复位后可 reset_target('halt')。未在实板执行整片擦除或 core-only reset，也未做长时间压力测试、多探针并发、拔线恢复。页大小仍需与芯片真实擦除布局匹配。SVD/信息展示中的芯片容量推断和完整总线扫描不在本轮修复范围。
