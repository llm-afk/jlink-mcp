# 2026-09-26 健壮性优化与离线回归验证

本次验证环境：Windows、Python 3.12.14、MCP 1.30.0、PyLink 2.0.1、Pydantic 2.13.5。

- **84 项自动化测试全部通过**，使用 `python -m unittest discover -s tests -v` 运行。
- 未连接目标板、未加载真实 J-Link DLL、未启动真实 GDB Server；涉及驱动行为的测试使用 fake DLL / 模拟进程。
- `pip check` 通过，Git diff 空白检查通过。
- 已配置 Windows / Linux、Python 3.10 / 3.12 的 GitHub Actions；本地未执行其他系统或 Python 版本的矩阵。

覆盖内容：

| 范围 | 验证结果 |
| --- | --- |
| Flash | 范围请求不再整片擦除；显式擦除、短读校验、分块读回、烧录校验失败、固件来源互斥 |
| 内存与寄存器 | 8/16/32 位访问与小端转换、短写、对齐和地址溢出、PC/SP/LR 别名、部分失败 |
| 调试 | 非法复位类型拒绝、内核复位策略在成功及异常时恢复 |
| MCP | 44 个工具注册、真实 stdio 初始化及调用、十六进制地址、严格参数类型、退出清理 |
| 执行调度 | 主循环响应、单线程串行、取消排队请求、运行中取消不引发硬件并发 |
| 连接与 GDB | 探针显式交接、就绪检测、输出消费、进程清理、掉线、关闭失败保留和重试确认 |
| RTT | 会话重置、缓冲区默认值、超时轮询、UTF-8 分块解码 |
| SVD | 外设和寄存器继承、本地覆盖、循环拒绝、8 位寄存器读取、缓存隔离及故障降级 |
| 发布包 | 构建 wheel，确认包含 4 个 SVD，并在源码目录之外导入和查询 |

注意：取消已经进入 DLL 的操作不会强制终止该操作；后续硬件调用会等待其完成。PyLink 2.0.1 的 16/32 位读取可能隐藏底层不完整单元，当前检查 SDK 实际返回的单元数量；Flash 校验使用 8 位读取，完整检查读回长度。真实硬件上的复位、擦除粒度和 GDB 兼容性仍需专项复测。

---

以下是历史真机测试记录；其中关于旧实现的遗留问题应结合上面的修复说明阅读。

# J-Link MCP 全量工具测试与修复报告

- 日期：2026-08-15
- 目标板：GD32C103CB（Cortex-M4，128 KB Flash / 32 KB RAM）
- 调试器：SEGGER J-Link V9.42（序列号 164000406，SWD，目标电压 3.329 V）
- 软件栈：pylink-square 2.0.1 + jlink-mcp（`pip install -e .` 可编辑安装）

---

## 1. 结论摘要

- 固件已成功烧录并校验：**app 141 + boot 133**（release 141 稳定运行，LED 正常闪烁）。
- **44 个 MCP 工具全部完成实测**，其中 43 个功能正常，1 个（`halt_cpu`）发现并修复了一个**严重 Bug**。
- 本次共发现并修复 **3 处 Bug**（1 处已在上一次提交 dc3b8cf 中修复，2 处在本次提交中修复）。
- 另有若干**遗留建议**（见第 6 节），非阻塞性。

---

## 2. 固件烧录

| 项 | 内容 |
|---|---|
| Boot | `dgm_boot_released_133.bin`（25600 B）→ Flash `0x08016800` |
| App | `dgm_app_released_141.bin`（40960 B）→ Flash `0x08000000` |
| 校验 | 两者 `matched: true` |

> ⚠️ 本次最初尝试烧录**最新 RTT 构建** `dgm_app_fw.bin`（41616 B），但该固件上电即 HardFault（XPSR=0x01000003，SP=0xFFFFFFD8），根因为实验性 RTT 构建的 packer 错误（target size 40960 < 原始 bin 41616）。经确认不影响 RTT 工具测试后，改用 **release 141**。
>
> 141 固件仍内建 RTT（`rtt_start` 可成功定位控制块），仅 channel 0 无下行输出。

---

## 3. 工具测试结果（44 个，按 9 类）

### 3.1 连接管理（5）
| 工具 | 结果 |
|---|---|
| `connect_device` | ✅ |
| `disconnect_device` | ✅ |
| `get_connection_status` | ✅ |
| `list_jlink_devices` | ✅ |
| `scan_target_devices` | ✅ |

### 3.2 设备信息（4）
| 工具 | 结果 |
|---|---|
| `get_target_info` | ✅ |
| `get_target_voltage` | ✅ |
| `get_cpu_state` | ✅ |
| `list_device_patches` | ✅ |

### 3.3 内存操作（6）
| 工具 | 结果 |
|---|---|
| `read_memory` | ✅ |
| `write_memory` | ✅ |
| `read_registers` | ✅（寄存器名需精确匹配，见 §6） |
| `write_register` | ✅ |
| `write_register_by_address` | ✅ |
| `read_register_by_address` | ✅ |

### 3.4 Flash 操作（4）
| 工具 | 结果 |
|---|---|
| `erase_flash` | ✅ |
| `erase_sector` | ✅（按页擦除 1 KB 成功） |
| `program_flash` | ✅（app/boot 均校验通过） |
| `verify_flash` | ✅（匹配 + 不匹配两种路径均验证） |

### 3.5 调试控制（6）
| 工具 | 结果 |
|---|---|
| `halt_cpu` | ❌ → **已修复**（见 §5.1） |
| `run_cpu` | ✅ |
| `reset_target` | ✅（normal/halt/core） |
| `step_instruction` | ✅ |
| `set_breakpoint` | ✅ |
| `clear_breakpoint` | ✅ |

### 3.6 RTT 日志（5）
| 工具 | 结果 |
|---|---|
| `rtt_start` | ✅ |
| `rtt_get_status` | ✅ |
| `rtt_read` | ✅（缓冲区为空，符合预期） |
| `rtt_write` | ✅（下行通道未配置，返回提示，符合预期） |
| `rtt_stop` | ✅ |

### 3.7 SVD 寄存器解析（6）
| 工具 | 结果 |
|---|---|
| `get_svd_peripherals` | ✅ |
| `get_svd_registers` | ✅ |
| `parse_register_value` | ✅ |
| `read_register_with_fields` | ✅ |
| `list_svd_devices` | ✅ |
| `match_chip_name` | ✅ |

### 3.8 GDB Server（3）
| 工具 | 结果 |
|---|---|
| `start_gdb_server` | ✅ |
| `get_gdb_server_status` | ✅ |
| `stop_gdb_server` | ✅ |

### 3.9 使用指南（5）
| 工具 | 结果 |
|---|---|
| `get_best_practices` | ✅ |
| `get_usage_guidance` | ✅ |
| `get_system_prompt` | ✅ |
| `get_forbidden_operations` | ✅ |
| `list_scenarios` | ✅ |

---

## 4. 发现的 Bug 及修复

### 5.1 `halt_cpu` 破坏性兜底（严重，本次修复）

**现象**：目标运行中调用 `halt_cpu` 后，PC 恒为复位向量 `0x08000168`，且运行中的固件被**复位**而非**暂停**。
（实测：`reset_target(normal)` 确认固件运行 → 等待 3 s 进入主循环 → `halt_cpu` 返回 `PC=0x08000168`。重复 3 次一致。）

**根因**：`debug.py` 中 `halt_cpu` 的兜底逻辑：

```python
jlink.halt()
for _ in range(100):
    if jlink.halted():
        break
    time.sleep(0.005)
else:
    # 兜底：reset + halt —— 会把 PC 重置到复位向量！
    jlink.reset(ms=0, halt=True)
```

当 `jlink.halt()` 无法让 `halted()` 返回 True 时，兜底 `reset(ms=0, halt=True)` 会把运行中的固件**复位**到复位向量，并把"复位"伪装成"暂停成功"。

**修复**（`jlink_mcp/tools/debug.py` + `jlink_mcp/exceptions.py`）：
- 移除破坏性的 `reset` 兜底。
- 以 `halted()` 为准轮询（最长 1 s）；若仍无法停止，抛出新增的 `HALT_FAILED`（错误码 303）明确报错，绝不复位。
- 修正注释：`halt()` 实为**同步**调用（pylink 的 `async_decorator` 仅在传入 `callback` 时才异步）。

### 5.2 `gdb_server.py` 缺少模块级 `import subprocess`（中等，本次修复）

`_cleanup()` 中引用了 `subprocess.TimeoutExpired`，但 `subprocess` 仅在 `start()` / `_find_jlink_gdbserver_exe()` 内部局部导入。当 GDB Server 进程未能在 2 s 内终止时，`except subprocess.TimeoutExpired` 会触发 `NameError`，掩盖真实的清理异常。

**修复**：在模块顶部补充 `import subprocess`。

### 5.3 Flash 校验不匹配列表截断（已在上次提交 dc3b8cf 修复）

大固件校验失败时会输出数 MB 的 mismatch JSON。已增加 `_MAX_MISMATCHES = 100` 截断，并返回 `mismatch_count` / `truncated` 字段。

---

## 6. 遗留问题与建议（非阻塞）

1. **`halt()` 底层根因待确认**：`JLINKARM_Halt()` 在此目标运行态下无法暂停（返回非零），`JLINKARM_IsHalted()` 持续返回 0。可能根因：① 该 DLL 版本 `JLINKARM_Halt()` 返回值约定变化；② 固件使能了调试期间不停计的看门狗（IWDG），暂停后立即被复位。修复后 `halt_cpu` 会**明确报错**而非静默复位，但"运行中暂停"这一能力在此固件上可能仍受限，需进一步裸 pylink 探测或固件侧检查确认。

2. **地址参数仅接受整数**：工具 schema 将 `address` 定义为 integer，需手动做十六进制→十进制转换，易出错（本次测试曾把 `0x08016800` 误算为 `0x08048200`，一度误判为"烧录失败"）。建议后续支持十六进制字符串地址。

3. **`read_registers` 寄存器名需精确匹配**：传 `["R0","PC","SP"]` 仅能读到 `R0`，实际名称为 `"R15 (PC)"` / `"R13 (SP)"`。建议增加别名映射（PC/SP/LR 等）。

4. **GDB Server 与 MCP 独占冲突**：jlink-mcp 持有 pylink 连接时，`start_gdb_server` 启动的独立 `JLinkGDBServerCL.exe` 进程可能无法连接同一 J-Link（USB 独占）。属设计限制，需在文档中说明或实现"启动 GDB Server 前先释放连接"。

5. **RTT 下行通道**：`rtt_write` 到 buffer 0 返回 0 字节，因 buffer 0 为上行通道（target→host）、固件未配置下行通道。属预期行为，非 Bug。

---

## 7. 变更清单

| 文件 | 变更 |
|---|---|
| `jlink_mcp/tools/debug.py` | `halt_cpu` 移除破坏性 reset 兜底，改为报错 |
| `jlink_mcp/exceptions.py` | 新增 `HALT_FAILED`（303）错误码 |
| `jlink_mcp/gdb_server.py` | 补充模块级 `import subprocess` |
| `jlink_mcp/tools/flash.py` | （上次提交）校验 mismatch 截断 |

> 注：因 jlink-mcp 为 `pip install -e .` 可编辑安装，上述修复需**重启 MCP server 进程后生效**，建议重启后对 `halt_cpu` 复测一次。
