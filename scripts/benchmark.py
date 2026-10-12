"""Bounded c1_driver host benchmarks; optional MCP reads never halt or write."""
import argparse
import asyncio
import cProfile
import hashlib
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def summary(times):
    ordered = sorted(times)
    return dict(median_ms=statistics.median(times), min_ms=ordered[0], max_ms=ordered[-1],
                count=len(times))


def offline(elf_path, profile_path, output):
    from jlink_mcp.symbols import SymbolFile
    from jlink_mcp.target_access import read_bytes
    from jlink_mcp.image_match import compare_image
    from jlink_mcp.profiles import load_profile
    elf = SymbolFile(str(elf_path))
    profile, _ = load_profile(profile_path)
    cases = {}
    def measure(name, call, count=40):
        call()
        times = []
        for _ in range(7):
            start = time.perf_counter()
            for _ in range(count):
                call()
            times.append((time.perf_counter() - start) * 1000 / count)
        cases[name] = summary(times)
        print(name, round(cases[name]['median_ms'], 4), 'ms', flush=True)
    for width in (8, 16, 32):
        values = list(range(65536 // (width // 8)))
        if width == 8:
            values = [v & 255 for v in values]
        probe = SimpleNamespace(memory_read=lambda *args, values=values, **kwargs: values)
        measure('convert_64k_' + str(width), lambda: read_bytes(probe, 0, 65536, width), 30)
    for name in ('MotorControl', 'MotorControl.Iq', 'MotorControl.PID_Iq.kp'):
        measure('resolve_' + name, lambda name=name: elf.variable(name))
    measure('readonly_sections', elf.readonly_sections)
    sections, _ = elf.readonly_sections()
    def memory(address, size):
        for section in sections:
            offset = address - section['address']
            if 0 <= offset and offset + size <= len(section['data']):
                return section['data'][offset:offset + size]
        raise ValueError('Unexpected image range')
    measure('compare_matching_image', lambda: compare_image(elf, profile, memory), 20)
    profiler = cProfile.Profile()
    profiler.runcall(lambda: [elf.variable('MotorControl') for _ in range(200)])
    profiler.dump_stats(str(output.with_suffix('.pstats')))
    return cases, elf.sha256


async def hardware(args, metadata):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    cases = {}
    params = StdioServerParameters(command=sys.executable, args=['-m', 'jlink_mcp'], cwd=str(REPO),
                                  env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONPATH=str(REPO)))
    with args.output.with_suffix('.server.log').open('w', encoding='utf-8') as log:
        async with stdio_client(params, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                assert len((await client.list_tools()).tools) == 8
                async def call(tool, request=None):
                    raw = await client.call_tool(tool, {'request': request} if request is not None else {})
                    result = json.loads(next(c.text for c in raw.content if c.type == 'text'))
                    if not result.get('success'):
                        metadata['failure'] = dict(tool=tool, request=request, result=result)
                        raise RuntimeError(str(result))
                    return result
                metadata['discovery'] = await call('discover')
                opened = await call('session', dict(action='open', serial_number=args.serial,
                                                  profile_path=str(args.profile), elf_path=str(args.elf),
                                                  speed_khz=args.speed_khz))
                sid = opened['session_id']
                metadata['opened'] = opened
                try:
                    assert opened['meta']['cpu_after'] == 'running'
                    matched = await call('firmware', dict(session_id=sid, action='verify_image'))
                    assert matched['status'] == 'matched'
                    metadata['image_match'] = matched
                    for width in (8, 16, 32):
                        requests = [dict(action='read', items=[dict(kind='memory', address=0x08000000,
                                                                  size=65536, width=width)])]
                        cases.update(await run_case(call, sid, 'read_64k_' + str(width), 'read', requests[0], args.count))
                    for name in ('MotorControl', 'MotorControl.Iq', 'MotorControl.PID_Iq.kp'):
                        request = dict(action='read', items=[dict(kind='variable', name=name)])
                        cases.update(await run_case(call, sid, 'typed_' + name, 'read', request, args.count))
                    cases.update(await run_case(call, sid, 'verify_image', 'firmware', dict(action='verify_image'), args.count))
                    metadata['final'] = await call('session', dict(action='status'))
                    assert metadata['final']['meta']['cpu_after'] == 'running'
                    requested = opened['meta']['target']['requested_speed_khz']
                    if isinstance(requested, int):
                        assert metadata['final']['connection']['data']['speed_khz'] == requested
                finally:
                    metadata['closed'] = await call('session', dict(action='close', session_id=sid))
    return cases


async def run_case(call, sid, name, tool, request, count):
    request = dict(session_id=sid, **request)
    await call(tool, request)
    times, durations = [], []
    for _ in range(count):
        start = time.perf_counter()
        result = await call(tool, request)
        times.append((time.perf_counter() - start) * 1000)
        assert result['meta']['cpu_after'] == 'running'
        durations.append(result['meta']['duration_ms'])
    stats = summary(times)
    stats['service_durations_ms'] = durations
    print('mcp_' + name, round(stats['median_ms'], 4), 'ms', flush=True)
    return {name: stats}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--profile', type=Path, default=REPO / 'profiles/c1_driver.json')
    parser.add_argument('--serial', help='Enables live read-only hardware measurements')
    parser.add_argument('--speed-khz', type=int, help='Explicit probe speed; otherwise use project profile')
    parser.add_argument('--count', type=int, choices=range(3, 31), default=15)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError(args.output)
    cases, sha = offline(args.elf, args.profile, args.output)
    report = dict(platform=platform.platform(), python=sys.version, elf_sha256=sha, offline=cases,
                  source_sha256={str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in (REPO / 'jlink_mcp').rglob('*.py')})
    try:
        if args.serial:
            report['metadata'] = {}
            report['hardware'] = asyncio.run(hardware(args, report['metadata']))
        report['success'] = True
    except Exception as exc:
        report.update(success=False, error=str(exc))
        raise
    finally:
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        print('EVIDENCE', args.output.resolve(), flush=True)


if __name__ == '__main__':
    main()
