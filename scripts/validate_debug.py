"""Validate debugging extensions on the user's current app, with Flash rollback on failure.

Requires power output disabled. Double-backs up full Flash, programs the verified
current BIN, preserves bytes outside its range, exercises break/watch/fault paths,
and leaves the new app running on success. No firmware source edits or fixture build.
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from jlink_mcp.symbols import SymbolFile

REPO = Path(__file__).resolve().parents[1]
APP = Path(r'C:\Users\32196\Desktop\c1_driver\Firmware_app\MDK-ARM\object\dgm_app_fw.axf')


async def validate(args):
    assert args.power_stage_off, 'Intrusive validation requires disabled power output'
    output = REPO / '.local' / 'validation' / ('debug_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    output.mkdir(parents=True)
    elf, binary = output / 'app.axf', output / 'app.bin'
    elf.write_bytes(args.elf.read_bytes())
    binary.write_bytes(args.elf.with_suffix('.bin').read_bytes())
    symbols = SymbolFile(str(elf))
    segments = [s for s in symbols.elf.iter_segments() if s['p_type'] == 'PT_LOAD']
    assert len(segments) == 1 and segments[0]['p_paddr'] == 0x08000000
    assert segments[0].data() == binary.read_bytes(), 'ELF and BIN do not describe the same load image'
    assert binary.stat().st_size < 117 * 1024
    ram_ends = [s['sh_addr'] + s['sh_size'] for s in symbols.elf.iter_sections()
                if s['sh_flags'] & 2 and 0x20000000 <= s['sh_addr'] < 0x20008000]
    assert ram_ends and max(ram_ends) <= 0x20007ff0, 'UDF scratch overlaps allocated RAM'
    assert int.from_bytes(binary.read_bytes()[:4], 'little') <= 0x20007ff0, 'UDF scratch overlaps initial stack top'
    trace, sid = [], None
    def save():
        (output / 'results.json').write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding='utf-8')
    def record(**entry):
        trace.append(entry)
        save()
    hashes = {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
              for folder in ('jlink_mcp', 'tests', 'profiles') for p in (REPO / folder).rglob('*')
              if p.is_file() and p.suffix in ('.py', '.json')}
    (output / 'source_hashes.json').write_text(json.dumps(hashes, indent=2), encoding='utf-8')
    record(check='current ELF/BIN identity', passed=True, elf_sha256=symbols.sha256,
           bin_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), size=binary.stat().st_size)
    params = StdioServerParameters(command=sys.executable, args=['-m', 'jlink_mcp'], cwd=str(REPO),
                                  env=dict(os.environ, PYTHONPATH=str(REPO), PYTHONIOENCODING='utf-8'))
    with (output / 'server.log').open('w', encoding='utf-8') as log:
        async with stdio_client(params, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                listed = (await client.list_tools()).tools
                assert len(listed) == 10
                (output / 'tools.json').write_text(json.dumps([t.model_dump() for t in listed], indent=2), encoding='utf-8')
                async def call(tool, request=None, ok=True):
                    nonlocal sid
                    response = await client.call_tool(tool, {'request': request} if request is not None else {})
                    result = json.loads(next(c.text for c in response.content if c.type == 'text'))
                    record(tool=tool, request=request, result=result)
                    print(tool, request.get('action', '') if request else '', result.get('success'), result.get('completion', result.get('status', '')), flush=True)
                    if result.get('session_lost'):
                        sid = None
                    if ok and not result.get('success'):
                        raise RuntimeError(str(result))
                    return result
                async def target(tool, ok=True, **request):
                    return await call(tool, dict(session_id=sid, **request), ok)
                async def opening():
                    result = await call('session', dict(action='open', profile_path=str(REPO / 'profiles/c1_driver.json'),
                                                       elf_path=str(elf), serial_number=args.serial))
                    return result['session_id']
                async def raw(address, size):
                    result = await target('read', items=[dict(kind='memory', address=address, size=size, width=32)])
                    return bytes.fromhex(result['items'][0]['data']['data_hex'])
                async def dbg(value):
                    await target('write', consistency='live', verify=True,
                                 item=dict(kind='memory', address=0xe0042004, width=32, data_hex=value.to_bytes(4, 'little').hex()))
                await call('discover')
                sid = await opening()
                original_dbg, backup, changed, passed = None, None, False, False
                scratch_backup = None
                try:
                    original_dbg = int.from_bytes(await raw(0xe0042004, 4), 'little')
                    await dbg(original_dbg | 256)
                    halted = await target('control', action='halt', ok=False)
                    if not halted['success']:
                        await target('control', action='reset', mode='halt')
                    backup, second = output / 'original.bin', output / 'original_second.bin'
                    for path in (backup, second):
                        await target('firmware', action='backup', address=0x08000000, size=131072, output_path=str(path))
                    assert backup.read_bytes() == second.read_bytes()
                    record(check='double full Flash backup', passed=True, sha256=hashlib.sha256(backup.read_bytes()).hexdigest())
                    # Preserve the unused tail of the last programmed erase page.
                    app = binary.read_bytes()
                    rounded = (len(app) + 1023) // 1024 * 1024
                    image = output / 'program.bin'
                    image.write_bytes(app + backup.read_bytes()[len(app):rounded])
                    await target('control', action='reset', mode='halt')
                    changed = True
                    await target('firmware', action='program', address=0x08000000, file_path=str(image))
                    await target('control', action='halt')
                    await target('firmware', action='verify', address=0x08000000, file_path=str(binary))
                    after = output / 'after_program.bin'
                    await target('firmware', action='backup', address=0x08000000, size=131072, output_path=str(after))
                    assert after.read_bytes()[len(app):] == backup.read_bytes()[len(app):]
                    record(check='all Flash outside current BIN preserved before app startup', passed=True)
                    await target('control', action='reset', mode='halt')
                    await target('firmware', action='verify_image')
                    main = await target('control', action='run_until', symbol='main', timeout_ms=3000)
                    assert main['completion'] == 'target_reached' and main['temporary_breakpoint_removed']
                    context = await target('inspect', action='context')
                    assert context['locations']['PC']['functions'][0]['name'] == 'main'
                    assert any(s['file'].endswith('main.c') for s in context['locations']['PC']['sources'])
                    assert context['instructions']
                    loop = await target('control', action='run_until', symbol='servo_loop', timeout_ms=3000)
                    assert loop['completion'] == 'target_reached'
                    await target('inspect', action='context')
                    wp = await target('breakpoint', action='set', kind='write', variable='MotorControl.BusVoltage')
                    await target('control', action='resume')
                    stopped = await target('control', action='wait', timeout_ms=1000)
                    assert any(r['name'] == 'data_watchpoint' for r in stopped['stop']['reasons'])
                    context = await target('inspect', action='context')
                    assert context['locations']['PC']['sources']
                    await target('breakpoint', action='remove', breakpoint_id=wp['breakpoint_id'])
                    for policy in ('halt', 'running'):
                        result = await target('control', action='run_until', symbol='HardFault_Handler', timeout_ms=30, on_timeout=policy, ok=False)
                        assert result['completion'] == 'timeout' and result['temporary_breakpoint_removed']
                    timed = await target('control', action='wait', timeout_ms=10, ok=False)
                    assert timed['completion'] == 'timeout' and timed['meta']['cpu_after'] == 'running'
                    await target('control', action='halt')
                    # Reset removes pending IRQ/watchdog context. Execute a defined
                    # Thumb UDF in unused top-of-SRAM, restoring those bytes below.
                    await target('control', action='reset', mode='halt')
                    await target('firmware', action='verify_image')
                    await target('control', action='run_until', symbol='main', timeout_ms=3000)
                    scratch = 0x20007ff0
                    scratch_backup = await raw(scratch, 4)
                    await target('write', verify=True, item=dict(kind='memory', address=scratch, width=32, data_hex='00de00bf'))
                    await target('firmware', action='verify_image')
                    bp = await target('breakpoint', action='set', symbol='HardFault_Handler')
                    await target('write', verify=True, item=dict(kind='register', name='PC', value=scratch))
                    await target('firmware', action='verify_image')
                    await target('control', action='resume')
                    stop = await target('control', action='wait', timeout_ms=1000)
                    assert stop['stop']['registers']['PC'] & ~1 == bp['address']
                    fault = await target('inspect', action='fault')
                    assert fault['exception_frame']['status'] == 'decoded', fault
                    assert fault['exception_frame']['registers']['PC'] == scratch, fault
                    await target('inspect', action='context')
                    record(check='current app run_until/source/watchpoint/timeouts/real exception frame', passed=True)
                    passed = True
                finally:
                    try:
                        if sid is None:
                            sid = await opening()
                        if changed and not passed:
                            await target('control', action='reset', mode='halt')
                            await target('firmware', action='program', address=0x08000000, file_path=str(backup))
                            await target('control', action='halt')
                            await target('firmware', action='verify', address=0x08000000, file_path=str(backup))
                            record(check='failure rollback to original full Flash', passed=True)
                        if original_dbg is not None:
                            await target('control', action='reset', mode='halt')
                            if scratch_backup is not None:
                                await target('write', verify=True, item=dict(kind='memory', address=0x20007ff0, width=32, data_hex=scratch_backup.hex()))
                            await dbg(original_dbg)
                            assert int.from_bytes(await raw(0xe0042004, 4), 'little') == original_dbg
                            if passed:
                                await target('firmware', action='verify', address=0x08000000, file_path=str(binary))
                                await target('firmware', action='verify_image')
                            await target('control', action='resume')
                            status = await call('session', dict(action='status'))
                            assert status['meta']['cpu_after'] == 'running'
                            record(check='final CPU running, original DBG_CTL restored', passed=True, dbg_ctl=original_dbg,
                                   image='current compiled app' if passed else 'original backup')
                    finally:
                        if sid:
                            await target('session', action='close')
                        save()
                        print('EVIDENCE', output, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--elf', type=Path, default=APP)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--power-stage-off', action='store_true')
    asyncio.run(validate(parser.parse_args()))
