import csv
import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from kaya_kopi.config import AppConfig
from kaya_kopi.repositories.invoice_deletion import delete_invoice, pending_invoice_deletions
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository


class InvoiceDeletionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = SQLiteOpsRepository(
            AppConfig(
                sqlite_db_path=root / "invoices.db",
                supplier_invoice_dir=root / "images",
                audited_csv_path=root / "master.csv",
            )
        )
        with self.repo._connect() as conn:
            for file_hash, number, filename in [
                ("one", "INV-1", "one.jpg"),
                ("two", "INV-2", "two.jpg"),
            ]:
                conn.execute(
                    """INSERT INTO supplier_invoices
                       (file_hash, invoice_number, filename, image_path)
                       VALUES (?, ?, ?, ?)""",
                    (file_hash, number, filename, filename),
                )
                conn.execute(
                    "INSERT INTO supplier_invoice_items (file_hash, line_no) VALUES (?, 1)",
                    (file_hash,),
                )
                (self.repo.cfg.supplier_invoice_dir / filename).write_bytes(b"image")
                (self.repo.cfg.supplier_invoice_dir / filename).with_suffix(".csv").write_text(
                    number, encoding="utf-8"
                )
        self.repo.cfg.audited_csv_path.write_text(
            "invoice_number,extra\nINV-1,one\nINV-2,two\nUNRELATED,keep\n", encoding="utf-8"
        )

    def tearDown(self):
        gc.collect()
        self.temp.cleanup()

    def test_deletes_invoice_and_preserves_unrelated_data(self):
        delete_chunks = Mock()
        delete_invoice(self.repo, "one", delete_chunks)
        delete_chunks.assert_called_once_with("one")
        self.assertFalse(self.repo.hash_exists("one"))
        self.assertTrue(self.repo.hash_exists("two"))
        self.assertEqual(self.repo.get_line_items("one"), [])
        self.assertEqual(len(self.repo.get_line_items("two")), 1)
        self.assertFalse((self.repo.cfg.supplier_invoice_dir / "one.jpg").exists())
        self.assertFalse((self.repo.cfg.supplier_invoice_dir / "one.csv").exists())
        self.assertTrue((self.repo.cfg.supplier_invoice_dir / "two.jpg").exists())
        self.assertTrue((self.repo.cfg.supplier_invoice_dir / "two.csv").exists())
        with self.repo.cfg.audited_csv_path.open(newline="", encoding="utf-8") as file:
            self.assertEqual(
                [row["invoice_number"] for row in csv.DictReader(file)], ["INV-2", "UNRELATED"]
            )
        self.assertEqual(pending_invoice_deletions(self.repo), [])

    def test_failure_is_recorded_and_cleanup_can_be_retried(self):
        delete_chunks = Mock(side_effect=[RuntimeError("Chroma unavailable"), None])
        with self.assertRaisesRegex(RuntimeError, "Chroma unavailable"):
            delete_invoice(self.repo, "one", delete_chunks)
        self.assertEqual(pending_invoice_deletions(self.repo)[0]["file_hash"], "one")
        delete_invoice(self.repo, "one", delete_chunks)
        self.assertEqual(pending_invoice_deletions(self.repo), [])
        self.assertEqual(delete_chunks.call_count, 2)

    def test_deletion_retries_superseded_chunk_cleanup(self):
        with self.repo._connect() as conn:
            conn.execute("INSERT INTO mysql_invoice_sync VALUES ('upload', 'one', 'INV-1', 1)")
            conn.execute("INSERT INTO superseded_invoice_chunks VALUES ('old', 'upload')")
        delete_chunks = Mock(side_effect=[RuntimeError("Chroma unavailable"), None, None])
        delete_mysql = Mock()
        with self.assertRaisesRegex(RuntimeError, "Chroma unavailable"):
            delete_invoice(self.repo, "one", delete_chunks, delete_mysql=delete_mysql)
        self.assertEqual(pending_invoice_deletions(self.repo)[0]["file_hash"], "one")

        delete_invoice(self.repo, "one", delete_chunks, delete_mysql=delete_mysql)

        self.assertEqual(delete_chunks.call_args_list, [(("old",),), (("old",),), (("one",),)])
        self.assertEqual(pending_invoice_deletions(self.repo), [])
        with self.repo._connect() as conn:
            self.assertEqual(conn.execute("SELECT * FROM superseded_invoice_chunks").fetchall(), [])

    def test_rejects_missing_invoice_or_shared_files_before_deletion(self):
        with self.assertRaisesRegex(ValueError, "No matching SQLite invoice"):
            delete_invoice(self.repo, "missing", Mock())
        with self.assertRaisesRegex(ValueError, "do not match"):
            delete_invoice(self.repo, "one", Mock(), "INV-2")
        with self.repo._connect() as conn:
            conn.execute("UPDATE supplier_invoices SET filename='one.jpg' WHERE file_hash='two'")
        with self.assertRaisesRegex(ValueError, "also referenced"):
            delete_invoice(self.repo, "one", Mock())
        self.assertTrue(self.repo.hash_exists("one"))
        self.assertTrue((self.repo.cfg.supplier_invoice_dir / "one.jpg").exists())
        self.assertEqual(pending_invoice_deletions(self.repo), [])

    def test_rejects_reupload_during_pending_cleanup(self):
        with self.assertRaises(RuntimeError):
            delete_invoice(self.repo, "one", Mock(side_effect=RuntimeError("Chroma unavailable")))
        with self.repo._connect() as conn:
            conn.execute(
                """INSERT INTO supplier_invoices
                   (file_hash, invoice_number, filename, image_path)
                   VALUES ('one', 'INV-NEW', 'one.jpg', 'one.jpg')"""
            )
        with self.assertRaisesRegex(ValueError, "re-uploaded"):
            delete_invoice(self.repo, "one", Mock())
        self.assertTrue(self.repo.hash_exists("one"))
        self.assertEqual(len(pending_invoice_deletions(self.repo)), 1)


if __name__ == "__main__":
    unittest.main()
