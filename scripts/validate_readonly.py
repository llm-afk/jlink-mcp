"""Read-only profile/image/DWARF validation through a fresh MCP stdio server."""
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

REPO = Path(__file__).resolve().parents[1]


async def validate(serial):
    output = REPO / '.local' / 'validation' / ('profile_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    output.mkdir(parents=True)
    trace = []
    def save():
        (output / 'results.json').write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding='utf-8')
    hashes = {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
              for folder in ('jlink_mcp', 'tests', 'profiles') for p in (REPO / folder).rglob('*')
              if p.is_file() and p.suffix in ('.py', '.json')}
    (output / 'source_hashes.json').write_text(json.dumps(hashes, indent=2), encoding='utf-8')
    params = StdioServerParameters(command=sys.executable, args=['-m', 'jlink_mcp'], cwd=str(REPO),
                                  env=dict(os.environ, PYTHONPATH=str(REPO), PYTHONIOENCODING='utf-8'))
    with (output / 'server.log').open('w', encoding='utf-8') as log:
        async with stdio_client(params, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
                assert len(tools) == 8
                (output / 'tools.json').write_text(json.dumps([t.model_dump() for t in tools], indent=2), encoding='utf-8')
                async def call(tool, request=None, require_success=True):
                    response = await client.call_tool(tool, {'request': request} if request is not None else {})
                    result = json.loads(next(c.text for c in response.content if c.type == 'text'))
                    trace.append(dict(tool=tool, request=request, result=result))
                    save()
                    print(tool, request.get('action', '') if request else '', result.get('success'), flush=True)
                    if require_success:
                        assert result.get('success'), result
                    return result
                await call('discover')
                opened = await call('session', dict(action='open', profile_path=str(REPO / 'profiles/c1_driver.json'), serial_number=serial))
                sid = opened['session_id']
                try:
                    initial = await call('session', dict(action='status'))
                    state = initial['meta']['cpu_after']
                    assert state == 'running', initial
                    preflight = await call('inspect', dict(session_id=sid, action='preflight'))
                    widths = await call('read', dict(session_id=sid, action='read', items=[
                        dict(kind='memory', address=0x08000000, size=32, width=width) for width in (8, 16, 32)]))
                    assert len({entry['data']['data_hex'] for entry in widths['items']}) == 1
                    match = await call('firmware', dict(session_id=sid, action='verify_image'), False)
                    assert match['status'] in ('matched', 'mismatch', 'partial'), match
                    for name in ('state_mcs', 'MotorControl', 'MotorControl.Iq', 'MotorControl.PID_Iq.kp'):
                        await call('inspect', dict(session_id=sid, action='variable', name=name))
                    matched = match['status'] == 'matched'
                    if not matched:
                        blocked = await call('read', dict(session_id=sid, action='read', items=[dict(kind='variable', name='state_mcs')]), False)
                        assert blocked['items'][0]['error']['code'] == 'IMAGE_NOT_MATCHED'
                    items = [dict(kind='variable', name=name, require_match=matched)
                             for name in ('state_mcs', 'MotorControl', 'MotorControl.Iq', 'MotorControl.PID_Iq.kp')]
                    result = await call('read', dict(session_id=sid, action='read', consistency='live', items=items))
                    from jlink_mcp.dwarf_variables import decode
                    for entry in result['items']:
                        data = entry['data']
                        assert decode(data['type'], bytes.fromhex(data['data_hex'])) == data['value']
                        assert data['elf_match'] == match['status']
                    snap = await call('read', dict(session_id=sid, action='snapshot', items=items))
                    await call('read', dict(session_id=sid, action='diff', snapshot_id=snap['snapshot_id']))
                    await call('read', dict(session_id=sid, action='sample', items=items, count=3, interval_ms=10,
                                            output_path=str(output / 'samples.jsonl')))
                    listed = await call('control', dict(session_id=sid, action='breakpoint_list'))
                    assert listed['breakpoints'] == {}
                    waited = await call('control', dict(session_id=sid, action='wait', timeout_ms=10), False)
                    assert waited['completion'] == 'timeout' and waited['meta']['cpu_after'] == state
                    final = await call('session', dict(action='status'))
                    assert final['meta']['cpu_after'] == state
                    after = await call('inspect', dict(session_id=sid, action='preflight'))
                    assert [c['value'] for c in preflight['checks']] == [c['value'] for c in after['checks']]
                    trace.append(dict(check='read-only widths/profile/image/typed read/snapshot/sample/control validation', passed=True,
                                      cpu_before=state, cpu_after=final['meta']['cpu_after'], image_status=match['status']))
                    save()
                finally:
                    await call('session', dict(action='close', session_id=sid))
    print('EVIDENCE', output, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--serial', required=True)
    asyncio.run(validate(parser.parse_args().serial))
