# 工程配置

`c1_driver.json` 对应本工作区相邻的 c1_driver 工程；路径相对于 JSON 文件解析，不依赖服务当前目录。

没有保存探针序列号，每次 session open 仍需指定 serial_number。可复制配置加入自己的序列号。chip/architecture 不允许与档案冲突；ELF/SVD/接口/探针可以显式覆盖，状态显示实际解析后的配置与档案哈希。

区域依据：GD32C103CB 128KB Flash；当前工程 main.h 的 EEPROM_PAGE=117、EEPROM_PAGE_COUNT=11 与 1KB 页大小；当前链接文件使用 32KB SRAM。未凭空划分 Boot/App，整个非参数 Flash 统一称为 firmware。更换工程、板型或链接布局时需更新档案。

配置为声明式数据，加载不会改写调试寄存器。debug_checks 用于 inspect preflight 的显式只读检查；只有确认读取无副作用的寄存器才应放入。regions 用于 ELF 只读映像校验和类型化读取边界，**不是 write/firmware 的全面访问控制表**。

配置内容（尤其地址范围和芯片型号）需与实际目标一致，不能把档案本身当作硬件识别证据。
