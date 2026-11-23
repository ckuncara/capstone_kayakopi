"""Verify the backfill script can be launched directly."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class BackfillEntrypointTest(unittest.TestCase):
    def test_dry_run_without_pythonpath(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        script = repository_root / "scripts" / "backfill_supplier_invoices.py"

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            image_directory = temporary_path / "invoices"
            image_directory.mkdir()
            (image_directory / "sample.png").write_bytes(b"sample")
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment["SUPPLIER_INVOICE_DIR"] = str(image_directory)
            environment["LOCAL_SQLITE_PATH"] = str(temporary_path / "test.sqlite3")

            result = subprocess.run(
                [sys.executable, "-I", str(script), "--dry-run"],
                cwd=temporary_directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[DRY-RUN] Pending: sample.png", result.stdout)
        self.assertIn("Done. Processed: 1", result.stdout)


if __name__ == "__main__":
    unittest.main()
