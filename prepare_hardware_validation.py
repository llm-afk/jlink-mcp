"""Create isolated Keil builds using c1_driver's current startup and SDK."""
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parent / 'hardware_validation'
src = Path(r'C:\Users\32196\Desktop\c1_driver\Firmware_app')
dst = root / 'Firmware_app'
shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns('object', 'list', '*.uvguix.*', '*.uvoptx'))
project = dst / 'MDK-ARM/dgm_app.uvprojx'
tree = ET.parse(project)
common = tree.find('.//TargetCommonOption')
common.find('AfterMake/RunUserProg2').text = '0'
tree.write(project, encoding='utf-8', xml_declaration=True)

# Separate minimal validation target: same device/startup/system source, no motor initialization.
tree = ET.parse(project)
target = tree.find('.//Target')
target.find('TargetName').text = 'jlink_validation'
common = target.find('TargetOption/TargetCommonOption')
common.find('OutputDirectory').text = '.\\validation_object\\'
common.find('OutputName').text = 'jlink_validation'
common.find('ListingPath').text = '.\\validation_list\\'
groups = target.find('Groups')
groups.clear()
group = ET.SubElement(groups, 'Group')
ET.SubElement(group, 'GroupName').text = 'Validation'
files = ET.SubElement(group, 'Files')
paths = [r'..\Source\validation_main.c', r'..\Source\SEGGER_RTT.c',
         r'..\CMSIS\GD\GD32C10x\Source\system_gd32c10x.c',
         r'..\GD32C10x_standard_peripheral\Source\gd32c10x_misc.c',
         r'..\CMSIS\GD\GD32C10x\Source\ARM\startup_gd32c10x.s']
for p in paths:
    f = ET.SubElement(files, 'File')
    ET.SubElement(f, 'FileName').text = p.split('\\')[-1]
    ET.SubElement(f, 'FileType').text = '2' if p.endswith('.s') else '1'
    ET.SubElement(f, 'FilePath').text = p
tree.write(project.with_name('jlink_validation.uvprojx'), encoding='utf-8', xml_declaration=True)
ses = Path(r'D:\Program Files\SEGGER\SEGGER Embedded Studio 8.24')
for name, sub in [('SEGGER_RTT.c', 'source/RTT'), ('SEGGER_RTT.h', 'include/RTT'), ('SEGGER_RTT_Conf.h', 'include/RTT')]:
    shutil.copy2(ses / sub / name, dst / 'Source' / name)
(dst / 'Source/validation_main.c').write_text(r'''#include "gd32c10x.h"
#include "SEGGER_RTT.h"

volatile unsigned int validation_ticks;
volatile unsigned int validation_rx_bytes;
volatile unsigned int validation_heartbeat;
volatile unsigned int validation_scratch = 0x13579BDF;

void SysTick_Handler(void) { validation_ticks++; }

__attribute__((noinline)) void validation_checkpoint(void) {
    validation_heartbeat++;
    __NOP();
}

int main(void) {
    unsigned int last = 0;
    char input[64];
    int count;
    validation_scratch = 0x13579BDF;
    SEGGER_RTT_Init();
    SEGGER_RTT_WriteString(0, "JLINK_MCP_VALIDATION_20260929 boot\n");
    SystemCoreClockUpdate();
    SysTick_Config(SystemCoreClock / 1000);
    for (;;) {
        count = SEGGER_RTT_Read(0, input, sizeof(input));
        if (count > 0) {
            validation_rx_bytes += count;
            SEGGER_RTT_WriteString(0, "ECHO:");
            SEGGER_RTT_Write(0, input, count);
        }
        if ((unsigned int)(validation_ticks - last) >= 1000) {
            last = validation_ticks;
            validation_checkpoint();
            SEGGER_RTT_WriteString(0, "JLINK_MCP_VALIDATION_20260929 alive\n");
        }
    }
}
''', encoding='utf-8')
print(root)
