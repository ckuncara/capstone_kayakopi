"""Regression test for launching the installed Streamlit entry point."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class StreamlitEntrypointTest(unittest.TestCase):
    def test_entrypoint_imports_package_from_copilot_directory(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        environment["OPENAI_API_KEY"] = "test-placeholder"
        environment["PYTHONPATH"] = str(repository_root / "src")
        with tempfile.TemporaryDirectory() as temporary_directory:
            environment["KAYA_CHAT_DB"] = str(
                Path(temporary_directory) / "chat-checkpoints.sqlite3"
            )
            result = subprocess.run(
                [sys.executable, "streamlit_app.py"],
                cwd=repository_root / "src" / "kaya_kopi" / "copilot",
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        combined_output = result.stdout + result.stderr
        self.assertNotIn("No module named 'kaya_kopi'", combined_output)
        self.assertEqual(result.returncode, 0, combined_output)

    def test_ocr_harness_imports_without_pythonpath(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        app = repository_root / "src" / "kaya_kopi" / "test_kaya_ocr_chroma.py"
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["OPENAI_API_KEY"] = ""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            environment["LOCAL_SQLITE_PATH"] = str(temporary_path / "test.sqlite3")
            environment["SUPPLIER_INVOICE_DIR"] = str(temporary_path / "invoices")
            code = (
                "import sys; from streamlit.testing.v1 import AppTest; "
                "app = AppTest.from_file(sys.argv[1], default_timeout=60).run(); "
                "assert not app.exception, str(app.exception); "
                "assert app.title[0].value == 'Kaya Kopi — Supplier Invoice OCR & ChromaDB Test Harness'"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-c", code, str(app)],
                cwd=temporary_directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
