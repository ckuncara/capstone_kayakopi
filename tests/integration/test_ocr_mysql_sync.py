"""Isolated MySQL tests; set KAYA_TEST_MYSQL_PORT to run."""

import os
import gc
import io
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import pymysql
import chromadb
from chromadb.api.types import EmbeddingFunction
from PIL import Image
from pymysql.cursors import DictCursor

from kaya_kopi.config import AppConfig
from kaya_kopi.copilot.invoice_upload import InvoiceUploadResult, upload_invoice
from kaya_kopi.ingestion.parsers.invoice_vision_audit import InvoiceOCRResult, validate_invoice_calculations
from kaya_kopi.ingestion.loader.mysql import Loader
from kaya_kopi.paths import project_root
from kaya_kopi.repositories.invoice_deletion import delete_invoice, pending_invoice_deletions
from kaya_kopi.repositories.mysql import DatabaseConfig, MySQLRepository
from kaya_kopi.repositories.mysql_invoice_sync import (
    _connect, delete_synced_invoice, pending_syncs, queue_resync, sync_invoice,
)
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository
from kaya_kopi.repositories.chroma_store import SharedChromaRepository
from kaya_kopi.security.budget_guard import BudgetGuard


@unittest.skipUnless(os.getenv("KAYA_TEST_MYSQL_PORT"), "isolated MySQL not configured")
class OCRMySQLSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = int(os.environ["KAYA_TEST_MYSQL_PORT"])
        cls.password = os.environ["KAYA_TEST_MYSQL_PASSWORD"]
        cls.database = "test_ocr_" + uuid.uuid4().hex
        cls.admin = pymysql.connect(
            host="127.0.0.1", port=cls.port, user="root", password=cls.password,
            cursorclass=DictCursor, autocommit=True,
        )
        with cls.admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{cls.database}`")
            cursor.execute(f"USE `{cls.database}`")
            cursor.execute("CREATE TABLE outlets (outlet_id INT PRIMARY KEY)")
            cursor.execute("""CREATE TABLE supplier_purchases (
                purchase_id INT PRIMARY KEY, supplier_code VARCHAR(10),
                supplier_name VARCHAR(120), invoice_no VARCHAR(24),
                total_amount DECIMAL(12,2), gst_treatment VARCHAR(30),
                purchase_date VARCHAR(20))""")
            schema = (project_root() / "src" / "kaya_kopi" / "ingestion" / "schema" / "schema.sql").read_text()
            for statement in schema[schema.index("CREATE TABLE"):].split(";"):
                if statement.strip():
                    cursor.execute(statement)

    @classmethod
    def tearDownClass(cls):
        with cls.admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{cls.database}`")
        cls.admin.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = SQLiteOpsRepository(AppConfig(
            sqlite_db_path=root / "invoice.db",
            supplier_invoice_dir=root / "images",
            audited_csv_path=root / "master.csv",
        ))
        self.connect_patch = patch(
            "kaya_kopi.repositories.mysql_invoice_sync._connect",
            lambda: pymysql.connect(
                host="127.0.0.1", port=self.port, user="root", password=self.password,
                database=self.database, cursorclass=DictCursor, autocommit=False,
            ),
        )
        self.connect_patch.start()
        self.number = "T-" + uuid.uuid4().hex[:16]
        self.supplier = "SUPPLIER-" + uuid.uuid4().hex[:12]
        self.invoice = InvoiceOCRResult(
            vendor_name=self.supplier, invoice_number=self.number, currency="USD",
            is_gst_inclusive=True, gst_rate_percent=10,
            line_items=[{"description": "test", "quantity": 1, "unit_price": 110, "line_total": 110}],
            subtotal=100, gst_amount=10, grand_total=110, raw_markdown_text="test",
        )

    def tearDown(self):
        self.connect_patch.stop()
        gc.collect()
        self.temp.cleanup()

    def save(self):
        file_hash = uuid.uuid4().hex * 2
        (self.repo.cfg.supplier_invoice_dir / "invoice.jpg").write_bytes(b"test image")
        self.repo.save_invoice(
            file_hash, None, "invoice.jpg", "invoice.jpg", self.invoice,
            validate_invoice_calculations(self.invoice), sync_mysql=True,
        )
        return file_hash, pending_syncs(self.repo)[0]["upload_id"]

    def query(self, sql, params=()):
        with self.admin.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else None

    def test_new_invoice_retry_currency_and_delete(self):
        file_hash, upload_id = self.save()
        sync_invoice(self.repo, upload_id)
        sync_invoice(self.repo, upload_id)
        self.assertEqual(pending_syncs(self.repo), [])
        rows = self.query(
            "SELECT * FROM supplier_invoice WHERE invoice_number=%s", (self.number,)
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["invoice_date"])
        self.assertEqual(rows[0]["currency"], "USD")
        reader = MySQLRepository(DatabaseConfig(
            host="127.0.0.1", port=self.port, user="root",
            password=self.password, database=self.database,
        ))
        result = reader.get_invoice(self.number)
        self.assertEqual(result["canonical_cost"]["currency"], "USD")
        self.assertEqual(result["canonical_cost"]["total"], "110.00")
        self.assertIsNone(result["canonical_cost"]["total_sgd"])
        spend = reader.analyze_suppliers("spend", self.invoice.vendor_name, None, None, None, "all")
        self.assertIn(self.number, [entry["invoice_number"] for entry in spend["excluded_invoices"]])
        self.assertGreaterEqual(spend["excluded_non_sgd_invoices"], 1)
        usd = next(entry for entry in spend["foreign_currency_results"] if entry["currency"] == "USD")
        self.assertGreaterEqual(float(usd["total"]), 110)
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.repo.check_sync_identity(file_hash, self.number, "OTHER SUPPLIER")
        delete_synced_invoice(upload_id)
        queue_resync(self.repo, upload_id)
        self.assertEqual(len(pending_syncs(self.repo)), 1)
        sync_invoice(self.repo, upload_id)
        delete_invoice(self.repo, file_hash, lambda _: None, delete_mysql=delete_synced_invoice)
        self.assertEqual(self.query(
            "SELECT supplier_invoice_id FROM supplier_invoice WHERE invoice_number=%s", (self.number,)
        ), ())
        self.assertEqual(pending_invoice_deletions(self.repo), [])

    def test_replace_linked_invoice_marks_review_and_preserves_existing_on_delete(self):
        file_hash, upload_id = self.save()
        source_key = uuid.uuid4().hex * 2
        self.query("""INSERT INTO source_document
            (source_version_key, source_uri, content_sha256, document_type, mime_type, extraction_status)
            VALUES (%s,'legacy',%s,'invoice','image/jpeg','OK')""", (source_key, source_key))
        source_id = self.query("SELECT source_document_id FROM source_document WHERE source_version_key=%s", (source_key,))[0]["source_document_id"]
        self.query("INSERT INTO supplier_purchases VALUES (1,'T',%s,%s,100,'EXCLUSIVE','01/01/2026')", (self.supplier, self.number))
        self.query("""INSERT INTO supplier_invoice
            (source_document_id,supplier_purchase_id,invoice_number,supplier_name,subtotal,
             gst_rate_percent,gst_amount,total_amount,arithmetic_valid,extraction_status)
            VALUES (%s,1,%s,%s,100,10,10,110,1,'OK')""", (source_id, self.number, self.supplier))
        invoice_id = self.query("SELECT supplier_invoice_id FROM supplier_invoice WHERE source_document_id=%s", (source_id,))[0]["supplier_invoice_id"]
        self.query("""INSERT INTO purchase_invoice_reconciliation
            (supplier_purchase_id,supplier_invoice_id,reconciliation_status,legacy_subtotal_sgd,validation_status)
            VALUES (1,%s,'MATCHED',100,'CLIENT_RULE_APPLIED')""", (invoice_id,))
        sync_invoice(self.repo, upload_id)
        reconciliation = self.query("SELECT * FROM purchase_invoice_reconciliation WHERE supplier_invoice_id=%s", (invoice_id,))[0]
        self.assertEqual(reconciliation["validation_status"], "REVIEW_REQUIRED")
        self.assertIsNone(reconciliation["scanned_subtotal_sgd"])
        self.query("INSERT INTO supplier (supplier_code,canonical_name,validation_status) VALUES ('T',%s,'VERIFIED')", (self.supplier,))
        loader = Loader(self.admin, project_root())
        loader.load_invoices(
            [{"source_file": "legacy.jpg", "invoice_number": self.number, "supplier_name": self.supplier}],
            [{"source_file": "legacy.jpg", "invoice_number": self.number}],
        )
        loader.load_purchase_invoice_reconciliation()
        self.assertEqual(
            self.query("SELECT validation_status FROM purchase_invoice_reconciliation WHERE supplier_invoice_id=%s", (invoice_id,))[0]["validation_status"],
            "REVIEW_REQUIRED",
        )
        self.assertEqual(len(self.query("SELECT * FROM supplier_invoice WHERE invoice_number=%s", (self.number,))), 1)
        delete_invoice(self.repo, file_hash, lambda _: None, delete_mysql=delete_synced_invoice)
        self.assertEqual(len(self.query("SELECT * FROM supplier_invoice WHERE supplier_invoice_id=%s", (invoice_id,))), 1)
        self.assertEqual(len(self.query("SELECT * FROM source_document WHERE source_document_id=%s", (source_id,))), 1)

    def test_failed_mysql_transaction_stays_pending_and_retries(self):
        self.invoice.line_items[0].description = "x" * 300
        _, upload_id = self.save()
        with self.assertRaises(pymysql.MySQLError):
            sync_invoice(self.repo, upload_id)
        self.assertEqual(len(pending_syncs(self.repo)), 1)
        self.assertEqual(self.query(
            "SELECT supplier_invoice_id FROM supplier_invoice WHERE invoice_number=%s", (self.number,)
        ), ())
        with self.repo._connect() as conn:
            conn.execute("UPDATE supplier_invoice_items SET description='fixed'")
        sync_invoice(self.repo, upload_id)
        self.assertEqual(pending_syncs(self.repo), [])

    def test_quality_issues_are_idempotent_and_removed_on_cleanup(self):
        self.invoice.line_items[0].line_total = 111
        _, upload_id = self.save()
        sync_invoice(self.repo, upload_id)
        quality = self.query(
            """SELECT q.issue_import_key,q.record_type,q.resolution_status
               FROM data_quality_issue q JOIN source_document d
                 ON d.source_document_id=q.source_document_id
               JOIN ocr_invoice_source s ON s.source_document_id=d.source_document_id
               WHERE s.upload_id=%s ORDER BY q.record_type""",
            (upload_id,),
        )
        self.assertEqual(
            {row["record_type"] for row in quality},
            {"supplier_invoice", "supplier_invoice_item"},
        )
        self.query(
            "UPDATE data_quality_issue SET resolution_status='RESOLVED' WHERE issue_import_key=%s",
            (quality[0]["issue_import_key"],),
        )
        sync_invoice(self.repo, upload_id)
        self.assertEqual(
            self.query(
                "SELECT resolution_status FROM data_quality_issue WHERE issue_import_key=%s",
                (quality[0]["issue_import_key"],),
            )[0]["resolution_status"],
            "RESOLVED",
        )
        delete_synced_invoice(upload_id)
        for issue in quality:
            self.assertEqual(
                self.query(
                    "SELECT issue_id FROM data_quality_issue WHERE issue_import_key=%s",
                    (issue["issue_import_key"],),
                ),
                (),
            )

    def test_upload_populates_invoice_stores_and_related_mysql_tables(self):
        self.invoice.currency = "SGD"
        self.invoice.invoice_date = "2025-01-11"
        self.invoice.line_items[0].line_total = 111
        code = uuid.uuid4().hex[:8].upper()
        purchase_id = 100000 + uuid.uuid4().int % 100000000
        self.query(
            "INSERT INTO supplier (supplier_code,canonical_name,validation_status) "
            "VALUES (%s,%s,'VERIFIED')",
            (code, self.supplier),
        )
        self.query(
            """INSERT INTO supplier_purchases
               (purchase_id,supplier_code,supplier_name,purchase_date,invoice_no,total_amount,gst_treatment)
               VALUES (%s,%s,%s,%s,%s,%s,'EXCLUSIVE')""",
            (purchase_id, code, self.supplier, "11/01/2025", self.number, 100),
        )

        class FixedEmbedding(EmbeddingFunction):
            def __init__(self):
                pass

            def __call__(self, input):
                return [[1.0, 0.0] for _ in input]

            @staticmethod
            def name():
                return "fixed_test_embedding"

        collection = chromadb.EphemeralClient().create_collection(
            f"invoice_upload_{uuid.uuid4().hex}", embedding_function=FixedEmbedding()
        )
        vectors = object.__new__(SharedChromaRepository)
        vectors.cfg = self.repo.cfg
        vectors.budget_guard = None
        vectors.get_collection = lambda: collection
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(buffer, format="PNG")
        raw = buffer.getvalue()
        with patch(
            "kaya_kopi.copilot.invoice_upload.extract_invoice_with_openai",
            return_value=self.invoice,
        ):
            result = upload_invoice(
                raw, self.repo.cfg, self.repo, vectors,
                BudgetGuard(self.repo.cfg.sqlite_db_path),
            )
        self.assertIsInstance(result, InvoiceUploadResult)
        self.assertEqual(result.sync.purchase_id, purchase_id)
        self.assertTrue(result.sync.review_required)
        file_hash = self.repo.compute_hash(raw)
        self.assertTrue(self.repo.hash_exists(file_hash))
        self.assertEqual(len(self.repo.get_line_items(file_hash)), 1)
        self.assertEqual(collection.get(ids=[file_hash])["ids"], [file_hash])
        self.assertEqual(pending_syncs(self.repo), [])
        row = self.query(
            """SELECT i.supplier_invoice_id, d.raw_text, s.upload_id
               FROM supplier_invoice i
               JOIN source_document d ON d.source_document_id=i.source_document_id
               JOIN ocr_invoice_sync s ON s.supplier_invoice_id=i.supplier_invoice_id
               JOIN ocr_invoice_source os ON os.source_document_id=d.source_document_id
               WHERE i.invoice_number=%s""",
            (self.number,),
        )[0]
        self.assertEqual(row["raw_text"], "test")
        self.assertEqual(len(self.query(
            "SELECT * FROM supplier_invoice_item WHERE supplier_invoice_id=%s",
            (row["supplier_invoice_id"],),
        )), 1)
        self.assertEqual(len(self.query(
            "SELECT * FROM purchase_invoice_reconciliation WHERE supplier_invoice_id=%s",
            (row["supplier_invoice_id"],),
        )), 1)
        self.assertEqual(len(self.query(
            """SELECT q.issue_id FROM data_quality_issue q
               JOIN ocr_invoice_source s ON s.source_document_id=q.source_document_id
               WHERE s.upload_id=%s""",
            (row["upload_id"],),
        )), 2)

    def test_fixed_invoice_removes_obsolete_upload_quality_issues(self):
        self.invoice.line_items[0].line_total = 111
        _, upload_id = self.save()
        sync_invoice(self.repo, upload_id)
        with self.repo._connect() as conn:
            conn.execute(
                "UPDATE supplier_invoices SET invoice_date='2025-01-11', "
                "audit_status='PASS', audit_issues='[]'"
            )
            conn.execute("UPDATE supplier_invoice_items SET is_valid=1, line_diff=0")
        sync_invoice(self.repo, upload_id)
        self.assertEqual(
            self.query(
                """SELECT q.issue_id FROM data_quality_issue q
                   JOIN ocr_invoice_source s ON s.source_document_id=q.source_document_id
                   WHERE s.upload_id=%s""",
                (upload_id,),
            ),
            (),
        )

    def test_mysql_cleanup_failure_is_retryable(self):
        file_hash, upload_id = self.save()
        sync_invoice(self.repo, upload_id)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            delete_invoice(
                self.repo, file_hash, lambda _: None,
                delete_mysql=lambda _: (_ for _ in ()).throw(RuntimeError("unavailable")),
            )
        self.assertEqual(len(pending_invoice_deletions(self.repo)), 1)
        delete_invoice(self.repo, file_hash, lambda _: None, delete_mysql=delete_synced_invoice)
        self.assertEqual(pending_invoice_deletions(self.repo), [])

    def test_created_invoice_with_purchase_link_is_not_deleted(self):
        file_hash, upload_id = self.save()
        sync_invoice(self.repo, upload_id)
        invoice_id = self.query(
            "SELECT supplier_invoice_id FROM supplier_invoice WHERE invoice_number=%s",
            (self.number,),
        )[0]["supplier_invoice_id"]
        self.query("INSERT INTO supplier_purchases VALUES (2,'U',%s,%s,100,'EXCLUSIVE','01/01/2026')",
                   (self.supplier, self.number))
        self.query("""INSERT INTO purchase_invoice_reconciliation
            (supplier_purchase_id,supplier_invoice_id,reconciliation_status,legacy_subtotal_sgd,validation_status)
            VALUES (2,%s,'MATCHED',100,'CLIENT_RULE_APPLIED')""", (invoice_id,))
        with self.assertRaisesRegex(ValueError, "linked to a purchase"):
            delete_invoice(self.repo, file_hash, lambda _: None, delete_mysql=delete_synced_invoice)
        self.assertEqual(len(pending_invoice_deletions(self.repo)), 1)
        self.assertEqual(len(self.query("SELECT * FROM supplier_invoice WHERE supplier_invoice_id=%s",
                                        (invoice_id,))), 1)
        self.query("DELETE FROM purchase_invoice_reconciliation WHERE supplier_purchase_id=2")
        delete_invoice(self.repo, file_hash, lambda _: None, delete_mysql=delete_synced_invoice)
        self.assertEqual(pending_invoice_deletions(self.repo), [])

    def test_inclusive_line_fallback_does_not_add_gst_twice(self):
        self.invoice.currency = "SGD"
        self.invoice.gst_amount = 9
        _, upload_id = self.save()
        sync_invoice(self.repo, upload_id)
        reader = MySQLRepository(DatabaseConfig(
            host="127.0.0.1", port=self.port, user="root",
            password=self.password, database=self.database,
        ))
        cost = reader.get_invoice(self.number)["canonical_cost"]
        self.assertEqual((cost["subtotal_sgd"], cost["gst_sgd"], cost["total_sgd"]),
                         ("100.00", "10.00", "110.00"))
        spend = reader.analyze_suppliers("spend", self.invoice.vendor_name, None, None, None, "all")
        self.assertEqual(spend["results"][0]["total_sgd"], "110.00")

    def test_connection_enables_strict_sql_mode(self):
        config = DatabaseConfig(
            host="127.0.0.1", port=self.port, user="root",
            password=self.password, database=self.database,
        )
        with patch("kaya_kopi.repositories.mysql_invoice_sync.DatabaseConfig.from_env", return_value=config):
            connection = _connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT @@sql_mode AS mode")
                    self.assertIn("STRICT_ALL_TABLES", cursor.fetchone()["mode"])
            finally:
                connection.close()

    def test_upload_matches_purchase_using_ingestion_rules_and_retry_is_idempotent(self):
        self.invoice.currency = "SGD"
        self.invoice.invoice_date = "2025-01-11"
        code = uuid.uuid4().hex[:8].upper()
        purchase_id = 100000 + uuid.uuid4().int % 100000000
        self.query(
            "INSERT INTO supplier (supplier_code,canonical_name,validation_status) VALUES (%s,%s,'VERIFIED')",
            (code, self.supplier),
        )
        self.query(
            """INSERT INTO supplier_purchases
               (purchase_id,supplier_code,supplier_name,purchase_date,invoice_no,total_amount,gst_treatment)
               VALUES (%s,%s,%s,%s,%s,%s,'EXCLUSIVE')""",
            (purchase_id, code, self.supplier, "11/01/2025", self.number, 200),
        )
        self.query(
            """INSERT INTO supplier_purchases
               (purchase_id,supplier_code,supplier_name,purchase_date,invoice_no,total_amount,gst_treatment)
               VALUES (%s,%s,%s,%s,%s,%s,'EXCLUSIVE')""",
            (purchase_id + 1, code, self.supplier, "10/01/2025", self.number, 100),
        )
        self.query(
            """INSERT INTO purchase_invoice_reconciliation
               (supplier_purchase_id,reconciliation_status,legacy_subtotal_sgd,validation_status)
               VALUES (%s,'NO_SCAN',200,'CLIENT_RULE_APPLIED')""",
            (purchase_id,),
        )
        _, upload_id = self.save()
        result = sync_invoice(self.repo, upload_id)
        self.assertEqual(result.purchase_id, purchase_id)
        self.assertFalse(result.review_required)
        row = self.query(
            """SELECT i.supplier_purchase_id,r.reconciliation_status,r.match_method,
                      r.scanned_subtotal_sgd,r.subtotal_difference_sgd,r.validation_status
               FROM supplier_invoice i JOIN purchase_invoice_reconciliation r
                 ON r.supplier_invoice_id=i.supplier_invoice_id
               WHERE i.invoice_number=%s""", (self.number,)
        )[0]
        self.assertEqual(row["supplier_purchase_id"], purchase_id)
        self.assertEqual(row["reconciliation_status"], "MATCHED")
        self.assertEqual(row["match_method"], "SUPPLIER_INVOICE_NUMBER_DATE")
        self.assertEqual(str(row["subtotal_difference_sgd"]), "-100.00")
        self.assertEqual(row["validation_status"], "CLIENT_RULE_APPLIED")
        document = self.query(
            """SELECT d.document_type,d.raw_text,li.description,li.line_number
               FROM supplier_invoice i JOIN source_document d ON d.source_document_id=i.source_document_id
               JOIN supplier_invoice_item li ON li.supplier_invoice_id=i.supplier_invoice_id
               WHERE i.invoice_number=%s""",
            (self.number,),
        )[0]
        self.assertEqual(document["document_type"], "supplier_invoice")
        self.assertEqual(document["raw_text"], "test")
        self.assertEqual((document["description"], document["line_number"]), ("test", 1))
        self.assertFalse(sync_invoice(self.repo, upload_id).review_required)
        self.assertEqual(
            self.query("SELECT validation_status FROM purchase_invoice_reconciliation WHERE supplier_purchase_id=%s",
                       (purchase_id,))[0]["validation_status"],
            "CLIENT_RULE_APPLIED",
        )
        with self.repo._connect() as conn:
            conn.execute(
                "UPDATE supplier_invoices SET ocr_subtotal=90, audit_status='FAIL (1 issues)', audit_issues='[\"changed\"]'"
            )
        queue_resync(self.repo, upload_id)
        self.assertTrue(sync_invoice(self.repo, upload_id).review_required)
        changed = self.query(
            """SELECT scanned_subtotal_sgd,subtotal_difference_sgd,validation_status
               FROM purchase_invoice_reconciliation WHERE supplier_purchase_id=%s""",
            (purchase_id,),
        )[0]
        self.assertEqual(str(changed["scanned_subtotal_sgd"]), "90.00")
        self.assertEqual(str(changed["subtotal_difference_sgd"]), "-110.00")
        self.assertEqual(changed["validation_status"], "REVIEW_REQUIRED")
        Loader(self.admin, project_root()).load_purchase_invoice_reconciliation()
        self.assertEqual(
            self.query("SELECT supplier_invoice_id FROM purchase_invoice_reconciliation WHERE supplier_purchase_id=%s",
                       (purchase_id,))[0]["supplier_invoice_id"],
            self.query("SELECT supplier_invoice_id FROM supplier_invoice WHERE invoice_number=%s",
                       (self.number,))[0]["supplier_invoice_id"],
        )

    def test_unmatched_upload_is_saved_and_reported_without_reconciliation(self):
        self.invoice.currency = "SGD"
        _, upload_id = self.save()
        result = sync_invoice(self.repo, upload_id)
        self.assertIsNone(result.purchase_id)
        self.assertIn("no available purchase", result.message)
        self.assertEqual(len(self.query("SELECT * FROM supplier_invoice WHERE invoice_number=%s",
                                        (self.number,))), 1)
        self.assertEqual(self.query(
            """SELECT r.reconciliation_id FROM purchase_invoice_reconciliation r
               JOIN supplier_invoice i ON i.supplier_invoice_id=r.supplier_invoice_id
               WHERE i.invoice_number=%s""", (self.number,)
        ), ())
        self.assertEqual(pending_syncs(self.repo), [])

    def test_creates_reconciliation_for_purchase_without_existing_row(self):
        self.invoice.currency = "SGD"
        self.invoice.invoice_date = "2025-01-11"
        code = uuid.uuid4().hex[:8].upper()
        purchase_id = 100000 + uuid.uuid4().int % 100000000
        self.query(
            "INSERT INTO supplier (supplier_code,canonical_name,validation_status) VALUES (%s,%s,'VERIFIED')",
            (code, self.supplier),
        )
        self.query(
            """INSERT INTO supplier_purchases
               (purchase_id,supplier_code,supplier_name,purchase_date,invoice_no,total_amount,gst_treatment)
               VALUES (%s,%s,%s,'11/01/2025',%s,100,'EXCLUSIVE')""",
            (purchase_id, code, self.supplier, self.number),
        )
        _, upload_id = self.save()
        result = sync_invoice(self.repo, upload_id)
        self.assertEqual(result.purchase_id, purchase_id)
        row = self.query(
            "SELECT * FROM purchase_invoice_reconciliation WHERE supplier_purchase_id=%s",
            (purchase_id,),
        )[0]
        self.assertEqual(row["reconciliation_status"], "MATCHED")
        self.assertEqual(str(row["subtotal_difference_sgd"]), "0.00")

    def test_purchase_already_matched_to_another_invoice_is_preserved(self):
        self.invoice.currency = "SGD"
        self.invoice.invoice_date = "2025-01-11"
        code = uuid.uuid4().hex[:8].upper()
        purchase_id = 100000 + uuid.uuid4().int % 100000000
        self.query(
            "INSERT INTO supplier (supplier_code,canonical_name,validation_status) VALUES (%s,%s,'VERIFIED')",
            (code, self.supplier),
        )
        self.query(
            """INSERT INTO supplier_purchases
               (purchase_id,supplier_code,supplier_name,purchase_date,invoice_no,total_amount,gst_treatment)
               VALUES (%s,%s,%s,'11/01/2025',%s,100,'EXCLUSIVE')""",
            (purchase_id, code, self.supplier, self.number),
        )
        source_key = uuid.uuid4().hex * 2
        self.query("""INSERT INTO source_document
            (source_version_key,source_uri,content_sha256,document_type,mime_type,extraction_status)
            VALUES (%s,'legacy',%s,'invoice','image/jpeg','OK')""", (source_key, source_key))
        source_id = self.query(
            "SELECT source_document_id FROM source_document WHERE source_version_key=%s", (source_key,)
        )[0]["source_document_id"]
        self.query("""INSERT INTO supplier_invoice
            (source_document_id,supplier_purchase_id,invoice_number,supplier_name,subtotal,
             gst_rate_percent,gst_amount,total_amount,arithmetic_valid,extraction_status)
            VALUES (%s,%s,'OTHER',%s,100,10,10,110,1,'OK')""",
            (source_id, purchase_id, self.supplier))
        other_id = self.query(
            "SELECT supplier_invoice_id FROM supplier_invoice WHERE source_document_id=%s", (source_id,)
        )[0]["supplier_invoice_id"]
        self.query("""INSERT INTO purchase_invoice_reconciliation
            (supplier_purchase_id,supplier_invoice_id,reconciliation_status,legacy_subtotal_sgd,validation_status)
            VALUES (%s,%s,'MATCHED',100,'CLIENT_RULE_APPLIED')""", (purchase_id, other_id))
        _, upload_id = self.save()
        result = sync_invoice(self.repo, upload_id)
        self.assertIsNone(result.purchase_id)
        self.assertEqual(self.query(
            "SELECT supplier_invoice_id FROM purchase_invoice_reconciliation WHERE supplier_purchase_id=%s",
            (purchase_id,),
        )[0]["supplier_invoice_id"], other_id)

    def test_foreign_currency_is_not_matched_against_sgd_purchase(self):
        self.invoice.invoice_date = "2025-01-11"
        code = uuid.uuid4().hex[:8].upper()
        purchase_id = 100000 + uuid.uuid4().int % 100000000
        self.query(
            "INSERT INTO supplier (supplier_code,canonical_name,validation_status) VALUES (%s,%s,'VERIFIED')",
            (code, self.supplier),
        )
        self.query(
            """INSERT INTO supplier_purchases
               (purchase_id,supplier_code,supplier_name,purchase_date,invoice_no,total_amount,gst_treatment)
               VALUES (%s,%s,%s,'11/01/2025',%s,100,'EXCLUSIVE')""",
            (purchase_id, code, self.supplier, self.number),
        )
        _, upload_id = self.save()
        result = sync_invoice(self.repo, upload_id)
        self.assertIsNone(result.purchase_id)
        self.assertIn("non-SGD", result.message)
        self.assertEqual(self.query(
            "SELECT * FROM purchase_invoice_reconciliation WHERE supplier_purchase_id=%s",
            (purchase_id,),
        ), ())

    def test_bad_purchase_data_rolls_back_invoice_and_can_be_retried(self):
        self.invoice.currency = "SGD"
        self.invoice.invoice_date = "2025-01-11"
        code = uuid.uuid4().hex[:8].upper()
        purchase_id = 100000 + uuid.uuid4().int % 100000000
        self.query(
            "INSERT INTO supplier (supplier_code,canonical_name,validation_status) VALUES (%s,%s,'VERIFIED')",
            (code, self.supplier),
        )
        self.query(
            """INSERT INTO supplier_purchases
               (purchase_id,supplier_code,supplier_name,purchase_date,invoice_no,total_amount,gst_treatment)
               VALUES (%s,%s,%s,'bad-date',%s,100,'EXCLUSIVE')""",
            (purchase_id, code, self.supplier, self.number),
        )
        _, upload_id = self.save()
        with self.assertRaisesRegex(ValueError, "invalid date or subtotal"):
            sync_invoice(self.repo, upload_id)
        self.assertEqual(self.query(
            "SELECT * FROM supplier_invoice WHERE invoice_number=%s", (self.number,)
        ), ())
        self.assertEqual(len(pending_syncs(self.repo)), 1)
        self.query("UPDATE supplier_purchases SET purchase_date='11/01/2025' WHERE purchase_id=%s",
                   (purchase_id,))
        self.assertEqual(sync_invoice(self.repo, upload_id).purchase_id, purchase_id)

    def test_ambiguous_match_does_not_overwrite(self):
        _, upload_id = self.save()
        for _ in range(2):
            source_key = uuid.uuid4().hex * 2
            self.query("""INSERT INTO source_document
                (source_version_key,source_uri,content_sha256,document_type,mime_type,extraction_status)
                VALUES (%s,'legacy',%s,'invoice','image/jpeg','OK')""", (source_key, source_key))
            source_id = self.query(
                "SELECT source_document_id FROM source_document WHERE source_version_key=%s",
                (source_key,),
            )[0]["source_document_id"]
            self.query("""INSERT INTO supplier_invoice
                (source_document_id,invoice_number,supplier_name,subtotal,gst_rate_percent,
                 gst_amount,total_amount,arithmetic_valid,extraction_status)
                VALUES (%s,%s,%s,1,10,0.1,1.1,1,'OK')""", (source_id, self.number, self.supplier))
        with self.assertRaisesRegex(ValueError, "Multiple MySQL invoices"):
            sync_invoice(self.repo, upload_id)
        self.assertEqual(len(pending_syncs(self.repo)), 1)
        self.assertEqual(len(self.query("SELECT * FROM supplier_invoice WHERE invoice_number=%s",
                                        (self.number,))), 2)

    def test_existing_schema_migration_preserves_data(self):
        database = "test_migration_" + uuid.uuid4().hex
        try:
            self.query(f"CREATE DATABASE `{database}`")
            self.query(f"USE `{database}`")
            self.query("CREATE TABLE source_document (source_document_id BIGINT PRIMARY KEY)")
            self.query("""CREATE TABLE supplier_invoice (
                supplier_invoice_id BIGINT PRIMARY KEY, supplier_address VARCHAR(255) NOT NULL,
                gst_registration_number VARCHAR(30) NOT NULL, invoice_date DATE NOT NULL,
                bill_to VARCHAR(255) NOT NULL)""")
            self.query("""INSERT INTO supplier_invoice VALUES (1,'address','gst','2025-01-01','bill')""")
            migration = (
                project_root() / "src" / "kaya_kopi" / "ingestion" / "schema" /
                "migrate_ocr_invoice_sync.sql"
            ).read_text()
            for statement in migration.split(";"):
                if statement.strip():
                    self.query(statement)
            row = self.query("SELECT * FROM supplier_invoice WHERE supplier_invoice_id=1")[0]
            self.assertEqual(row["currency"], "SGD")
            self.assertEqual(row["supplier_address"], "address")
            self.query("UPDATE supplier_invoice SET invoice_date=NULL, supplier_address=NULL")
        finally:
            self.query(f"USE `{self.database}`")
            self.query(f"DROP DATABASE `{database}`")
