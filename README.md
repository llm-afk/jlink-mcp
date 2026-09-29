# J-Link MCP 0.5.1

面向嵌入式调试任务的 MCP 服务。对外只提供 **10 个工具**；J-Link DLL、ELF 符号解析和 SVD 是内部实现。0.3 是破坏性 API 更新，旧工具名不再注册，迁移见 [迁移说明](docs/MIGRATION.md)。

## 安装与启动

Python 3.10+，SEGGER J-Link 软件。建议在仓库虚拟环境安装：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m jlink_mcp
```

若 `py` 不可用，请使用已安装 Python 的绝对路径。MCP 客户端的 `command` 建议使用 `.venv\Scripts\python.exe` 的绝对路径，`args` 为 `["-m", "jlink_mcp"]。

可设置 `JLINK_LIB_PATH` 指向 `JLink_x64.dll` 或其目录；`JLINK_SVD_DIR` 指向自定义 SVD 目录。默认打包 GD32C10x、N32H473/474/475 四份 SVD。

SDK 固定兼容范围 `mcp>=1.26.0,<2`；MCP 2.x 更改了 FastMCP API。ELF 符号解析使用 pyelftools，不要求 GDB。

## 十个接口

除 discover 外，各工具参数统一放在 `request` 对象中。操作相关字段由严格的 action/kind 联合模型约束，多余参数被拒绝，不会静默忽略。

| 工具 | 操作 |
|---|---|
| discover | 枚举探针、可用 SVD、能力边界，不连接目标 |
| session | open / status / close |
| read | 批量 memory / register / peripheral / symbol / variable |
| write | 单次 memory / register / peripheral 写入，可显式校验 |
| control | halt / resume / reset / step（指令级）/ wait / run_until |
| breakpoint | set / list / remove，执行断点及读/写/访问观察点 |
| inspect | symbol / variable / preflight / svd / context / fault |
| capture | snapshot / diff / sample |
| firmware | verify_image / program / verify / backup / erase_pages / erase_chip |
| channel | RTT open / status / read / write / close |

使用提示是 `debug://guide` 资源，不占工具列表。

## 基本流程

1. `discover()` 得到序列号和精确 SVD 名称。
2. 调用 `session`：

```json
{"request":{"action":"open","chip":"GD32C103CB","serial_number":"实际探针序列号","interface":"SWD","architecture":"cortex-m","svd_device":"GD32C10x"}}
```

需要符号时同时传入 `elf_path`。返回的 `session_id` 用于后续所有目标操作，重连后旧 ID 失效。仅允许一个活动会话；所有工具共享锁，包含采样和 RTT 等待。

3. 运行中读取，不自动暂停：

```json
{"request":{"session_id":"返回的ID","consistency":"live","items":[{"kind":"memory","address":536870912,"size":16,"width":32},{"kind":"symbol","name":"g_state"}]}}
```

`symbol` 只读取 ELF 中有明确地址和大小的对象原始字节；`g_state` 为示意符号，不保证存在于实际固件。需要 DWARF 类型和结构体成员时使用下面的 `variable`。ELF 的 SHA256 仅标识本地文件，初始 `target_match=unknown`；运行 `firmware verify_image` 才生成匹配证据。

4. 需要寄存器或单步时，先显式 `control`：

```json
{"request":{"session_id":"返回的ID","action":"halt","timeout_ms":1000}}
```

再调用 `read`：

```json
{"request":{"session_id":"返回的ID","consistency":"halted","items":[{"kind":"register","name":"PC"},{"kind":"register","name":"SP"}]}}
```

`live` 拒绝 CPU 寄存器读取，即使它当前已暂停；`halted` 要求已经暂停。读取过程中观察到 CPU 恢复运行时丢弃该批暂停快照。暂停 CPU 也不保证 DMA/外设静止。

5. 使用 `control resume` 恢复；结束调用 `session close`。不会因 close 自动恢复运行。断点只列出和清理本会话设置的断点。

## 项目配置与带类型的变量读取（0.4）

仓库提供 [c1_driver 配置](profiles/c1_driver.json)，包含芯片、SWD、SVD、ELF 路径、Flash/RAM 区域及看门狗调试位检查。相对文件路径以配置所在目录为基准；使用 wheel 安装时需自行保存项目 JSON，仓库示例不会自动安装到用户工程。配置加载不写寄存器；探针序列号由调用者明确指定。芯片和架构不允许与配置冲突。

```json
{"request":{"action":"open","profile_path":"C:/Users/32196/Desktop/jlink-mcp/profiles/c1_driver.json","serial_number":"174504233"}}
{"request":{"session_id":"返回的ID","action":"preflight"}}
{"request":{"session_id":"返回的ID","action":"verify_image"}}
```

后两个请求分别发给 `inspect` 和 `firmware`。`preflight` 只报告配置中的调试寄存器值、掩码和 satisfied；success 表示读取完成，不代表调试条件全部满足，也不自动设置看门狗冻结位。

`verify_image` 按 ELF 的 PT_LOAD 装载地址比较 allocated/read-only PROGBITS，排除可写 RAM 初始化段、NOBITS 和配置的可变参数区。返回 `matched / mismatch / partial / unknown`、比较字节数、各范围 SHA256 和最多 16 个差异地址。只有全部候选只读字节均被覆盖且一致才是 matched；这是指定只读区域的当次证据，不是完整 Flash、DWARF 调试信息或源码构建来源的证明。当前 c1 配置的 EEPROM 117–127 页不参与比较。

```json
{"request":{"session_id":"返回的ID","action":"variable","name":"MotorControl.PID_Iq.kp"}}
{"request":{"session_id":"返回的ID","consistency":"live","items":[{"kind":"variable","name":"state_mcs"},{"kind":"variable","name":"MotorControl.Iq"},{"kind":"variable","name":"MotorControl"}]}}
```

分别发给 `inspect`、`read`。`inspect variable` 只解析地址和类型；`read variable` 默认要求本会话已有 matched 证据。显式 `require_match=false` 可进行不匹配调查，结果仍标记实际 elf_match，不能据此确认变量语义。所有类型读取必须落在配置的 RAM/Flash 内；配置不是通用写入访问控制表。

支持具有固定地址的全局变量、嵌套结构体、连续定长数组、整数、浮点、布尔和枚举。`global.member[index]` 是唯一选择语法；指针只显示地址，不解引用。拒绝局部变量、动态位置、函数调用、位域、联合体、动态/跨步数组等未支持类型。单对象最大 64KB，数组最多 4096 元素，类型深度最多 16，解码值树最多 8192 节点。返回值同时保留原始字节；NaN/Infinity 用字符串表达，保持合法 JSON。

`capture` 直接复用 variable，无新增工具。运行中的多变量或结构体读取不保证一致时刻。显式写入、复位、Flash 修改尝试和连接失效会清除映像匹配证据；外部工具或目标自行改写 Flash 无法自动检测，必要时重新 verify_image。

## 等待、现场定位和数据观察点（0.5）

`control wait` 只等待 CPU 停止，最长 30 秒，不继续、不暂停、不复位。返回停止原因（调试请求、代码断点、数据观察点或向量捕获）、原始原因码和 PC/LR/SP/XPSR；原因读取失败会保留错误，不推断原因。

`control run_until` 要求已经暂停，接收 address 或函数 symbol，设置临时硬件断点并继续运行。返回 target_reached、already_at_target、stopped_elsewhere 或 timeout。默认 on_timeout=halt；显式 running 则不主动暂停。目标已停在指定地址时不执行任何指令。临时断点在正常返回及异常路径清理，复用用户已有断点时保留；清理失败返回剩余 breakpoint_id。DLL 挂死/进程终止时不能保证清理，仍以 BACKEND_TIMEOUT 的未知完成状态为准。

```json
{"request":{"session_id":"返回的ID","action":"run_until","symbol":"servo_loop","timeout_ms":3000,"on_timeout":"halt"}}
{"request":{"session_id":"返回的ID","action":"context","instructions":8}}
```

分别发给 control 和 inspect。`context` 要求暂停，给出 PC/LR 对应的 ELF 函数、源码路径/行号，并从目标实际字节反汇编；可选 address 指定反汇编起点，instructions=0 可跳过。最多 32 条，读取限定在项目内存或 ELF 可执行区。源码路径来自 DWARF，不代表源码文件内容已验证；映射始终携带 ELF 匹配状态，不将不匹配映射当成事实。停机原因的 unit_index=-1 表示驱动没有报告单元索引。

```json
{"request":{"session_id":"返回的ID","action":"set","kind":"write","variable":"MotorControl.BusVoltage"}}
{"request":{"session_id":"返回的ID","action":"resume"}}
{"request":{"session_id":"返回的ID","action":"wait","timeout_ms":1000}}
```

分别发给 breakpoint、control、control。breakpoint 的 kind 支持 execute/read/write/access；execute 为默认值。数据观察点接收 variable 或 address+size；宽度为 1/2/4 字节且要求对齐，匹配对应访问宽度，不解引用指针，也不支持任意值条件。观察点数量取决于硬件资源，分配失败明确返回错误。硬件报告的停止 PC 可能已位于触发访问之后；不能直接把当前行当成写入行。

所有执行断点均使用硬件，不回退软件 Flash 断点。符号执行断点和变量观察点要求本会话有 matched 证据；调查不匹配镜像时可显式使用地址。set/remove 仍要求暂停，reset/close 清理本会话持有的两类断点。resume 后若已经命中断点，返回 observed_halted 和 running_observed=false，避免将快速命中误报为超时。

`inspect fault` 新增具名 CFSR/HFSR 标志和 exception_frame。仅对 ARMv7-M 的有效 EXC_RETURN 解析一层异常帧，支持 MSP/PSP、基本/扩展浮点栈布局和对齐填充，保留核心寄存器原始字节。自动恢复要求停在向量表指向的异常入口；执行过处理函数后 SP 可能已移动，须同时提供明确的 frame_address 和 exc_return。帧必须落在配置 RAM 中；堆栈错误或字段无效时返回 unavailable/invalid。浮点载荷不解码，也不将异常帧冒充完整调用栈。

## 行为约定

- `read` 最多 64 项，合计最多 64KB，逐项结果包含采集时间、原始数据或明确错误。`width=8/16/32` 控制真实访问宽度，大小按字节计。
- 内存读取不会主动暂停；SVD 读取对已解析的 `readAction` 默认拒绝，需显式 `allow_side_effects=true`。SVD 不是完整访问策略：其继承、厂商扩展及遗漏不能作为无副作用证明。
- `write` 默认要求暂停，但不主动暂停；默认不读回，`verify=true` 才执行读回。外设读回可能有副作用或因 W1C、自清零、硬件并发变化而不等于写入值。
- 写入、运行控制等命令遇到异常不自动重试。返回失败时，尤其超时，不能据此推断目标没有发生变化。
- J-Link DLL 在独立子进程中运行。普通操作最多等待 20 秒，control/channel 至少 10 秒或请求等待时间加 5 秒，firmware 120 秒；超时终止本服务拥有的驱动进程树，返回 BACKEND_TIMEOUT、session_lost=true、completion=unknown。MCP 服务继续存活，必须显式重新打开会话，不能重发未知结果的写入。
- `control` 返回观测到的运行/暂停状态或 timeout；不会通过自动复位解决暂停失败。`step`、`run_until` 和硬件断点要求明确配置为 Cortex-M 的会话。
- `inspect fault` 读取并核对 CPUID，当前只支持 Cortex-M3/M4/M7。保留原始证据和逐项错误，不把 sticky fault 位当成当前根因。
- 每个结果含会话、目标、generation、UTC 时间、耗时、操作前后 CPU 状态。批量读取不是原子快照。

## 快照与有限采样

`capture snapshot` 使用与 `read` 相同的 items/consistency，返回 snapshot_id。`capture diff` 将该快照与现在比较；原快照保持不变。最多保留 16 个，超出淘汰最早项。

`capture sample` 使用相同 items，外加 `count`、`interval_ms`、可选 `output_path`。最多 128 次、1MiB 原始载荷和 10 秒请求窗口；单次底层驱动阻塞可能延长实际时长。默认文件在当前目录 `captures/`，JSONL 含原始结果、会话及主机时间，不覆盖已有文件。主机时间不是 MCU 精确采样时间，不能用于声称验证了高速电流环时序。

复位、固件修改尝试、断开/重连会使旧快照失效。固件修改失败也失效，因为可能已部分写入。

## 固件与 RTT

`verify_image` 可在运行中只读执行；其他 firmware 操作要求先明确暂停。

- program/verify：仅接收原始 `.bin` 和明确地址；最大 16MiB；program 必须读回校验，不预先整片擦除，不自动运行。
- backup：给出 address、size、output_path，不覆盖；返回文件 SHA256。失败可能留下不完整文件，只有 success=true 才是完整备份；单次读取和文件哈希不等于双读一致验证。
- erase_pages：必须显式提供页大小，地址必须页对齐；不会向下取整。页大小须与真实芯片匹配，实际擦除仍由 J-Link 算法决定。
- erase_chip：必须显式 `confirm_chip` 等于会话 chip；包含 bootloader/app，绝不能用于范围擦除的回退。
- Flash 算法可能改变 CPU 状态，结果报告实际状态；不承诺恢复操作前状态。
- RTT 沿用部分发送补发和 UTF-8 增量解码；默认读取立即返回，等待最长 30 秒。复位、Flash 修改、断连后重新 open。

## 架构和当前范围

`server.py` 是 MCP 适配层，`api_models.py` 是严格请求模型，`worker.py`/`worker_entry.py` 隔离原生驱动并执行墙钟超时，`service.py` 负责会话/串行执行/证据与任务操作，现有 `tools/` 和 `target_access.py` 提供底层实现，`symbols.py`/`dwarf_variables.py` 提供离线 ELF/DWARF 索引，`source_context.py` 提供源码映射和 Capstone Thumb 反汇编，`fault_context.py` 恢复有限异常栈，`profiles.py` 定义项目配置，`image_match.py` 比对只读映像。

当前不实现 GDB 调用栈、源码单步、完整 DWARF、RISC-V 故障分析、多目标会话。能力列表明确给出这些限制。原 GDB Server 模块保留供后续后端整合，未注册启停 MCP 工具；检测到它占用探针时拒绝 DLL 操作。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

保留全部 114 项回归测试，覆盖严格请求模型、驱动隔离、会话状态、Flash/RTT、DWARF、等待/观察点与异常现场。实板结果和清理说明见 [验证记录](docs/VALIDATION.md)。

只读实板复验（不暂停、不复位、不烧录）：

```powershell
.\.venv\Scripts\python.exe scripts/validate_readonly.py --serial 174504233
```

`scripts/validate_debug.py` 用于完整侵入性测试，需要明确传入 `--power-stage-off`，会备份、烧录当前 App 并注入可控故障；见验证记录。

## 目录

- `jlink_mcp/`：运行源码及 4 份 SVD。
- `tests/`：离线回归测试。
- `profiles/`：项目配置示例。
- `scripts/`：只读、完整调试两份实板验证脚本。
- `docs/`：迁移说明与验证记录。
- `pyproject.toml`：唯一的依赖和构建声明，安装使用 `pip install -e .`。

`.venv/` 是当前运行环境；`.local/` 保存本地历史归档和新生成的验证数据，均不提交仓库。复制的固件工程、旧审计脚本和重复报告已从工作目录清理，历史恢复材料在 `.local/workspace-before-cleanup-20260929.zip` 中。

MIT，原作者与许可见 LICENSE。
