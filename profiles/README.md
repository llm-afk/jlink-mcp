# 工程配置

`c1_driver.json` 对应本工作区相邻的 c1_driver 工程；路径相对于 JSON 文件解析，不依赖服务当前目录。

没有保存探针序列号，每次 session open 仍需指定 serial_number。可复制配置加入自己的序列号。chip/architecture 不允许与档案冲突；ELF/SVD/接口/探针/速率可以显式覆盖，状态显示实际解析后的配置与档案哈希。

`speed_khz` 为 auto 或固定 kHz。当前 c1_driver 配置设为 10000（10 MHz），2026-10-12 在该探针/目标上通过只读和完整调试回归；20 MHz 连接及 CPUID 读取失败，经工程用户同意改测 10 MHz。一次所有测试速率（含 100 kHz）都无法连接的故障经重新上电恢复，随后通过 6 次连续重连及一分钟持续读取。这不是其他工程的默认值或其他硬件的能力证明。固定速率不符会报错，不自动降速。未声明 speed_khz 的旧配置保持 auto。

区域依据：GD32C103CB 128KB Flash；当前工程 main.h 的 EEPROM_PAGE=117、EEPROM_PAGE_COUNT=11 与 1KB 页大小；当前链接文件使用 32KB SRAM。未凭空划分 Boot/App，整个非参数 Flash 统一称为 firmware。更换工程、板型或链接布局时需更新档案。

配置为声明式数据，加载不会改写调试寄存器。debug_checks 用于 inspect preflight 的显式只读检查；只有确认读取无副作用的寄存器才应放入。regions 用于 ELF 只读映像校验和类型化读取边界，**不是 write/firmware 的全面访问控制表**。

配置内容（尤其地址范围和芯片型号）需与实际目标一致，不能把档案本身当作硬件识别证据。
