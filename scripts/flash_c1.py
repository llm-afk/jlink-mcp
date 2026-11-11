"""Program a C1 application from its matching AXF/BIN pair and verify readback."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate_debug import APP, validate


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--elf', type=Path, default=APP, help='Application AXF and same-stem raw BIN')
    parser.add_argument('--serial', required=True, help='J-Link serial number')
    parser.add_argument('--power-stage-off', action='store_true', help='Motor power output is physically disabled')
    asyncio.run(validate(parser.parse_args(), exercise_debug=False))
