"""Build and exercise the wheel outside the source checkout, without hardware."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


class PackagingTests(unittest.TestCase):
    def test_wheel_contains_and_loads_bundled_svd(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            shutil.copytree(source / "jlink_mcp", project / "jlink_mcp",
                            ignore=shutil.ignore_patterns("__pycache__", ".svd_cache"))
            for filename in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(source / filename, project / filename)
            build = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", "from setuptools.build_meta import build_wheel; build_wheel('dist')"],
                cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            wheel = next((project / "dist").glob("*.whl"))
            installed = root / "installed"
            with zipfile.ZipFile(wheel) as archive:
                svd_files = {Path(name).name for name in archive.namelist() if name.endswith(".svd")}
                self.assertEqual(svd_files, {"GD32C10x.svd", "N32H473.svd", "N32H474.svd", "N32H475.svd"})
                archive.extractall(installed)
            environment = dict(os.environ, PYTHONPATH=str(installed),
                               LOCALAPPDATA=str(root / "cache"), XDG_CACHE_HOME=str(root / "cache"))
            environment.pop("JLINK_SVD_DIR", None)
            check = subprocess.run([sys.executable, "-X", "utf8", "-c", """
from jlink_mcp.svd_manager import svd_manager
assert len(svd_manager.device_names) == 4, svd_manager.device_names
register = svd_manager.get_register('N32H473', 'DMA1', 'DMA_CHCFG2')
assert register is not None and len(register.fields) == 14
assert svd_manager.get_register('GD32C10x', 'NVIC', 'IPR0').size == 8
"""], cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


if __name__ == "__main__":
    unittest.main()
