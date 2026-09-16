from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from llm_test_generation.terminate_package_processes import (
    terminate_package_processes,
)


class TerminatePackageProcessesTests(unittest.TestCase):
    def test_terminates_process_whose_cwd_is_package_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "package"
            repository.mkdir()
            process = subprocess.Popen(["sleep", "60"], cwd=repository)
            try:
                terminated = terminate_package_processes(repository, grace_seconds=1.0)
                process.wait(timeout=2)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

            self.assertIn(process.pid, terminated)
            self.assertNotEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
