from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class LauncherHardeningTests(unittest.TestCase):
    def test_hostile_cwd_and_python_environment_cannot_shadow_agent_memory(self) -> None:
        root = Path(__file__).resolve().parents[1]
        launcher = root / "amem"
        private_entrypoint = root / ".venv" / "bin" / "amem"
        self.assertTrue(launcher.is_file())
        self.assertTrue(private_entrypoint.is_file(), "launcher contract requires the private venv")

        with tempfile.TemporaryDirectory() as temporary:
            hostile_root = Path(temporary)
            hostile_package = hostile_root / "agent_memory"
            hostile_package.mkdir()
            marker = hostile_root / "hostile-imported"
            hostile_package.joinpath("__init__.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "Path(os.environ['AMEM_HOSTILE_MARKER']).write_text('imported', encoding='utf-8')\n"
                "raise RuntimeError('hostile package was imported')\n",
                encoding="utf-8",
            )

            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(hostile_root)
            environment["PYTHONHOME"] = str(hostile_root / "fake-python-home")
            environment["AMEM_HOSTILE_MARKER"] = str(marker)
            completed = subprocess.run(
                [str(launcher), "--version"],
                cwd=hostile_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(completed.stdout.strip().startswith("amem "))
            self.assertFalse(marker.exists(), completed.stderr)


if __name__ == "__main__":
    unittest.main()
