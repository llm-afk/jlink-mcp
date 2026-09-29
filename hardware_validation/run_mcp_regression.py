"""Physical validation through MCP stdio; backs up and restores main Flash.

Requires power stage OFF. Uses only MCP tools, not direct pylink calls.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
TRACE = []

def sha(data):
    return hashlib.sha256(data).hexdigest()

def save():
    (ROOT / 'regression_results.json').write_text(json.dumps(TRACE, ensure_ascii=False, indent=2), encoding='utf-8')

async def main():
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONIOENCODING='utf-8', JLINK_LIB_PATH=r'C:\Program Files\SEGGER\JLink\JLink_x64.dll')
    params = StdioServerParameters(command=sys.executable, args=['-m', 'jlink_mcp'], env=env, cwd=str(REPO))
    with (ROOT / 'regression_mcp_server.log').open('w', encoding='utf-8') as err:
        async with stdio_client(params, errlog=err) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                async def call(name, **args):
                    response = await session.call_tool(name, args)
                    result = json.loads(next(c.text for c in response.content if c.type == 'text'))
                    summary = dict(result)
                    if isinstance(summary.get('data'), list):
                        content = bytes(summary.pop('data'))
                        summary['length'] = len(content)
                        summary['sha256'] = sha(content)
                        summary.pop('hex_dump', None)
                    safe_args = {k: (v if k != 'data' or len(v) < 200 else f'<{len(v)} hex chars>') for k, v in args.items()}
                    TRACE.append({'tool': name, 'args': safe_args, 'result': summary})
                    save()
                    print(name, json.dumps(summary, ensure_ascii=True)[:450], flush=True)
                    if not result.get('success', True):
                        raise RuntimeError(f'{name} failed: {result}')
                    return result

                await call('connect_device', serial_number='174504233', interface='SWD', chip_name='GD32C103CB')
                changed = False
                backup = None
                try:
                    await call('reset_target', reset_type='halt')
                    assert (await call('get_cpu_state'))['halted']
                    async def read_flash():
                        return b''.join([bytes((await call('read_memory', address=0x08000000 + offset, size=65536, width=8))['data']) for offset in (0, 65536)])
                    backup = await read_flash()
                    assert len(backup) == 131072
                    assert await read_flash() == backup
                    first_backup = ROOT / 'original_flash_08000000_128k.bin'
                    if first_backup.exists() and first_backup.read_bytes() != backup:
                        old = first_backup.read_bytes()
                        changes = [0x08000000+i for i, (a, b) in enumerate(zip(old, backup)) if a != b]
                        TRACE.append({'check': 'changes since first backup after original firmware ran', 'count': len(changes), 'first': hex(min(changes)), 'last': hex(max(changes))}); save()
                    backup_path = ROOT / ('original_flash_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.bin')
                    backup_path.write_bytes(backup)
                    backup_path.with_suffix('.sha256').write_text(sha(backup) + '\n', encoding='ascii')
                    fixture = ROOT / 'Firmware_app/MDK-ARM/validation_object/jlink_validation.bin'
                    changed = True
                    result = await call('program_flash', address=0x08000000, file_path=str(fixture), verify=True)
                    assert result['verify_result']['matched']

                    # Exercise true sector erase with live neighbour preservation checks.
                    pages = bytes([0xA5])*1024 + bytes([0x3C])*1024 + bytes([0x5A])*1024
                    result = await call('program_flash', address=0x0801F400, data=pages.hex(), verify=True)
                    assert result['verify_result']['matched']
                    await call('erase_sector', address=0x0801F800, count=1, page_size=1024)
                    actual = bytes((await call('read_memory', address=0x0801F400, size=3072, width=8))['data'])
                    assert actual == pages[:1024] + b'\xff'*1024 + pages[2048:]
                    TRACE.append({'check': 'sector erase + both neighbours', 'passed': True}); save()

                    await call('reset_target', reset_type='normal')
                    await asyncio.sleep(1.2)
                    await call('rtt_start', timeout_ms=1000)
                    await asyncio.sleep(0.3)
                    first = await call('rtt_read', size=2048)
                    assert 'JLINK_MCP_VALIDATION_20260929' in first['data']
                    token = 'MCP_PING\n'
                    tx = await call('rtt_write', data=token)
                    assert tx['bytes_written'] == len(token)
                    await asyncio.sleep(0.2)
                    echo = await call('rtt_read', size=2048)
                    assert 'ECHO:' + token in echo['data']
                    long_token = 'LONG_' + '0123456789' * 6 + '\n'
                    tx = await call('rtt_write', data=long_token)
                    assert tx['bytes_written'] == len(long_token) and tx['complete']
                    await asyncio.sleep(0.1)
                    echo = await call('rtt_read', size=2048)
                    cleaned = echo['data'].replace('ECHO:', '').replace('JLINK_MCP_VALIDATION_20260929 alive\n', '')
                    assert long_token in cleaned
                    await call('rtt_write', data='中文\n')
                    await asyncio.sleep(0.1)
                    decoded = ''
                    for _ in range(20):
                        part = await call('rtt_read', size=1, timeout_ms=0)
                        decoded += part['data']
                        if '中文\n' in decoded:
                            break
                    assert '中文\n' in decoded
                    await call('disconnect_device')
                    assert not (await call('rtt_get_status'))['started']
                    await call('connect_device', serial_number='174504233', interface='SWD', chip_name='GD32C103CB')
                    await call('rtt_start', block_address=0x20000014)
                    await call('rtt_stop')
                    await call('rtt_stop')

                    await call('halt_cpu')
                    before = await call('get_cpu_state')
                    assert before['halted'] and before['pc'] != 0
                    await call('read_registers')
                    await call('step_instruction')
                    assert (await call('get_cpu_state'))['halted']
                    map_text = next((ROOT / 'Firmware_app/MDK-ARM/validation_list').glob('*.map')).read_text(errors='replace')
                    def symbol(name):
                        return int(re.search(r'^\s*' + re.escape(name) + r'\s+0x([0-9a-fA-F]+)', map_text, re.M).group(1), 16)
                    address = symbol('validation_checkpoint') & ~1
                    scratch = symbol('validation_scratch')
                    for width in (8, 16, 32):
                        await call('write_memory', address=scratch, data='78 56 34 12', width=width)
                        assert (await call('read_memory', address=scratch, size=4, width=width))['data'] == [0x78, 0x56, 0x34, 0x12]
                    assert (await call('read_register_by_address', address=scratch))['raw_value'] == 0x12345678
                    await call('set_breakpoint', address=address)
                    try:
                        await call('run_cpu')
                        await asyncio.sleep(1.4)
                        stopped = await call('get_cpu_state')
                        assert stopped['halted'] and stopped['pc'] == address
                    finally:
                        await call('clear_breakpoint', address=address)
                    await call('run_cpu')
                    await call('start_gdb_server', speed=1000)
                    try:
                        reader, writer = await asyncio.open_connection('127.0.0.1', 2331)
                        writer.write(b'$qSupported#37')
                        await writer.drain()
                        reply = await asyncio.wait_for(reader.readuntil(b'#'), timeout=3)
                        assert b'PacketSize=' in reply
                        writer.write(b'+')
                        await writer.drain()
                        writer.close()
                        await writer.wait_closed()
                        TRACE.append({'check': 'GDB default device and localhost handshake', 'reply': reply.decode(), 'passed': True}); save()
                    finally:
                        await call('stop_gdb_server')
                    TRACE.append({'check': 'fixture validation complete', 'passed': True}); save()
                finally:
                    if changed and backup is not None:
                        print('RESTORING ORIGINAL FLASH', flush=True)
                        await call('reset_target', reset_type='halt')
                        restored = await call('program_flash', address=0x08000000, file_path=str(backup_path), verify=True)
                        assert restored['verify_result']['matched'], 'RESTORE VERIFY FAILED'
                        check = b''.join([bytes((await call('read_memory', address=0x08000000 + offset, size=65536, width=8))['data']) for offset in (0, 65536)])
                        assert check == backup, 'RESTORE FULL READBACK FAILED'
                        TRACE.append({'check': 'original Flash restored byte-for-byte', 'sha256': sha(check), 'passed': True}); save()
                    await call('reset_target', reset_type='normal')
                    await call('get_cpu_state')
                    await call('disconnect_device')

asyncio.run(main())
