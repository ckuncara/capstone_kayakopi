import pytest
import pymysql
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from PIL import Image
from openpyxl import Workbook

from kaya_kopi.upload_assistant.storage import CSV_SOURCE_COLUMNS, ROSTER_SOURCE_COLUMNS, Settings, UploadStore, citation_path, collect_sources, select_sql
from kaya_kopi.upload_assistant.agent import UploadAgent, history_outlet, safe_result, with_source_warning
from kaya_kopi.upload_assistant.ingestion import InvoiceExtraction, RosterBatch, RosterRow, UploadProcessor, parse_outlet_csv, roster_sheets, roster_source_fields
from kaya_kopi.security.budget_guard import BudgetGuard


def test_archive_manifest_and_sources_use_data_type_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("KAYA_PROJECT_ROOT", str(tmp_path))
    store = UploadStore(Settings(tmp_path / "data" / "upload_assistant"))
    digest, source = store.archive("../../invoice.png", b"image", "invoices")
    assert source == "data/images/supplier_invoices/invoice.png"
    store.record(digest, source, "invoice.png", "invoices", 1)
    assert store.existing(digest)["source_file"] == source
    assert collect_sources({"rows": [{"source_file": source}, {"source_uri": source}]}) == [source]
    assert citation_path("/etc/passwd") is None
    assert citation_path("data/missing.png") is None
    assert len(store.files()) == 1


def test_conversations_persist_sources_usage_and_names(tmp_path, monkeypatch):
    monkeypatch.setenv("KAYA_PROJECT_ROOT", str(tmp_path))
    settings = Settings(tmp_path / "data" / "upload_assistant")
    store = UploadStore(settings)
    messages = [
        {"role": "user", "content": "Check invoices for test@example.com"},
        {"role": "assistant", "content": "Invoice evidence.", "sources": ["data/invoice.jpg"], "usage_caption": "Tokens: 120 | Estimated cost: $0.01", "turn_id": "turn-1"},
    ]
    store.save_conversation("first", messages)
    store.rename_conversation("first", "Invoice review")
    store.save_conversation("first", messages)
    reopened = UploadStore(settings)
    assert reopened.list_conversations() == [{"thread_id": "first", "name": "Invoice review"}]
    history = reopened.load_conversation("first")
    assert "test@example.com" not in history[0]["content"]
    assert history[1]["sources"] == ["data/invoice.jpg"]
    assert history[1]["usage_caption"] == messages[1]["usage_caption"]
    reopened.save_conversation("second", [{"role": "user", "content": "Another question"}])
    reopened.delete_conversation("first")
    assert reopened.load_conversation("first") is None
    assert reopened.load_conversation("second")
    with pytest.raises(ValueError):
        reopened.rename_conversation("second", " ")
    with pytest.raises(ValueError):
        reopened.rename_conversation("missing", "Name")


@pytest.mark.parametrize("kind,filename,folder", [
    ("csvs", "daily.csv", "tabular"), ("rosters", "roster.xlsx", "tabular"),
    ("texts", "note.txt", "docs/whatsapp_exports"),
    ("texts", "mail.eml", "docs/emails"),
    ("emails", "mail.txt", "docs/emails"),
    ("emails", "mail.eml", "docs/emails"),
])
def test_archive_reuses_matching_original_and_preserves_name_collisions(tmp_path, monkeypatch, kind, filename, folder):
    monkeypatch.setenv("KAYA_PROJECT_ROOT", str(tmp_path))
    store = UploadStore(Settings(tmp_path / "data" / "upload_assistant"))
    _, source = store.archive(filename, b"original", kind)
    assert source == f"data/{folder}/{filename}"
    assert store.archive(filename, b"original", kind)[1] == source
    _, other_source = store.archive(filename, b"changed", kind)
    assert other_source != source
    assert citation_path(source).read_bytes() == b"original"
    assert citation_path(other_source).read_bytes() == b"changed"


def test_settings_reuse_existing_database_and_collection(monkeypatch, tmp_path):
    monkeypatch.setenv("KAYA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("UPLOAD_AI_DATABASE", "kaya_uploads")
    monkeypatch.setenv("UPLOAD_AI_COLLECTION", "kaya_upload_assistant")
    monkeypatch.setenv("CHROMA_COLLECTION", "kaya_kopi_knowledge")
    settings = Settings.from_env()
    from kaya_kopi.repositories.mysql import DatabaseConfig
    assert settings.database == DatabaseConfig.from_env().database
    assert settings.collection == "kaya_kopi_knowledge"


def test_mysql_initialization_never_creates_a_database(tmp_path, monkeypatch):
    monkeypatch.setenv("KAYA_PROJECT_ROOT", str(tmp_path))
    store = UploadStore(Settings(tmp_path / "data" / "upload_assistant"))
    repository = MagicMock()
    store.repository = Mock(return_value=repository)
    store.init_mysql()
    cursor = repository._connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert len([statement for statement in statements if statement.startswith("CREATE TABLE IF NOT EXISTS uploaded_")]) == 3
    assert any("ADD COLUMN `mon_shift`" in statement for statement in statements)
    assert any("ADD COLUMN `hourly_rate_sgd`" in statement for statement in statements)
    assert all("CREATE DATABASE" not in statement for statement in statements)


@pytest.mark.parametrize("sql", [
    "DELETE FROM uploaded_invoices", "SELECT * FROM supplier_invoice",
    "SELECT * FROM mysql.user", "SELECT * FROM uploaded_invoices; SELECT 1",
    "WITH hidden AS (SELECT * FROM secret) SELECT * FROM hidden",
    "SELECT load_file('/etc/passwd') FROM uploaded_invoices",
])
def test_upload_query_rejects_unsafe_sql(sql):
    with pytest.raises(ValueError):
        select_sql(sql, {"uploaded_invoices"}, "mysql")


def test_upload_query_allows_aggregates_and_ctes():
    assert "SUM" in select_sql("WITH monthly AS (SELECT total_amount FROM uploaded_invoices) SELECT SUM(total_amount) FROM monthly", {"uploaded_invoices"}, "mysql")


@pytest.fixture
def processor(tmp_path, monkeypatch):
    monkeypatch.setenv("KAYA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "copilot_chat.sqlite3"))
    processor = UploadProcessor.__new__(UploadProcessor)
    processor.store = UploadStore(Settings(tmp_path / "data" / "upload_assistant"))
    processor.store.init_mysql = Mock()
    processor.store.repository = Mock(return_value=MagicMock())
    processor.structured = Mock()
    processor.client = Mock()
    processor.budget = Mock()
    processor.budget.usage_tracker.get_all_usage_history.return_value = []
    processor.budget.usage_tracker.get_unpriced_count.return_value = 0
    processor.budget.usage_tracker.get_turn_usage.return_value = None
    processor.log = Mock()
    return processor


@pytest.mark.parametrize("header,values,expected_date", [
    ("Date,Total Sales (SGD),No of Cups,Remarks", "01/07/2025,815.51,263,rainy", "2025-07-01"),
    ("date,sales$,txns", "2025-07-01,$815.51,263", "2025-07-01"),
    ("DAY,REVENUE,CUPS,WASTAGE_KG", "01-Jul-25,815.51,263,1.25", "2025-07-01"),
    ("Business Date,Gross Takings,Customer Count,Weather", "07/01/2025,815.51,263,Rain", "2025-07-01"),
])
def test_python_csv_parser_supports_all_client_layouts(header, values, expected_date):
    from decimal import Decimal
    row = parse_outlet_csv("daily_summary_AMK-01.csv", f"{header}\n{values}\n".encode())[0]
    assert row["outlet_name"] == "AMK-01"
    assert row["report_date"] == expected_date
    assert row["revenue_sgd"] == Decimal("815.51")
    assert row["reported_volume"] == 263
    assert row["volume_measure"] == "receipts"
    assert row["cups_sold"] is None
    assert row["milk_wastage_liters"] is None
    assert row["source_row_number"] == 2
    if "WASTAGE_KG" in header:
        assert row["wastage_kg"] == Decimal("1.25")


def test_python_csv_parser_flags_bad_values_without_inventing_zeroes():
    row = parse_outlet_csv("daily_summary_AMK-03.csv", b"date,sales$,txns\nnot-a-date,NaN,not-a-count\n")[0]
    assert row["report_date"] is None
    assert row["revenue_sgd"] is None
    assert row["reported_volume"] is None
    assert "invalid report date" in row["notes"]
    assert "invalid sales amount" in row["notes"]


def test_invoice_ocr_archives_original_and_saves_provenance(processor):
    image = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(image, format="PNG")
    processor.structured.return_value = InvoiceExtraction(
        vendor_name="Coffee supplier", invoice_number="INV-1", invoice_date="2025-08-01",
        outlet_name=None, currency="SGD", subtotal=40, gst_amount=3.6, total_amount=43.6,
        review_notes="Not audited", line_items=[],
    )
    result = processor.process("invoice.png", image.getvalue(), "invoices")
    assert result["record_count"] == 1
    assert citation_path(result["source_file"]).read_bytes() == image.getvalue()
    cursor = processor.store.repository.return_value._connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    values = cursor.execute.call_args.args[1]
    assert values[-1] == result["source_file"]
    assert values[4] is None
    processor.structured.reset_mock()
    assert processor.process("same.png", image.getvalue(), "invoices")["duplicate"]
    processor.structured.assert_not_called()


def test_csv_is_parsed_without_openai_and_missing_stays_null(processor):
    content = ("outlet,date,revenue\n" + "AMK-03,2025-08-01,42\n" * 45).encode()
    result = processor.process("reports.csv", content, "csvs")
    assert result["record_count"] == 45
    processor.structured.assert_not_called()
    processor.client.chat.completions.create.assert_not_called()
    processor.client.beta.chat.completions.parse.assert_not_called()
    processor.client.embeddings.create.assert_not_called()
    cursor = processor.store.repository.return_value._connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    values = cursor.executemany.call_args.args[1]
    assert len(values) == 45
    assert values[0][3] is None
    assert values[0][-1] == result["source_file"]


def test_csv_processing_does_not_require_or_initialize_openai(processor, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with patch("kaya_kopi.upload_assistant.ingestion.openai_client", side_effect=AssertionError("CSV must not initialize OpenAI")) as client_factory:
        local_processor = UploadProcessor(processor.store)
        result = local_processor.process("daily_summary_AMK-01.csv", b"Date,Total Sales (SGD),No of Cups,Remarks\n01/07/2025,815.51,263,\n", "csvs")
        assert result["record_count"] == 1
        assert "client" not in local_processor.__dict__
        client_factory.assert_not_called()


@pytest.mark.parametrize("content", [
    b"date,revenue\n2025-08-01,42,unexpected\n",
    b"date,date,revenue\n2025-08-01,2025-08-01,42\n",
    b"unknown,columns\n1,2\n",
])
def test_csv_structural_errors_do_not_save_or_call_openai(processor, content):
    with pytest.raises(ValueError):
        processor.process("daily_summary_AMK-03.csv", content, "csvs")
    processor.store.init_mysql.assert_not_called()
    processor.structured.assert_not_called()
    assert processor.store.files() == []


def test_unsupported_csv_saves_nothing_and_does_not_fall_back_to_openai(processor):
    with pytest.raises(ValueError, match="Unsupported CSV"):
        processor.process("reports.csv", b"outlet,date\nAMK-03,2025-08-01", "csvs")
    processor.store.init_mysql.assert_not_called()
    assert processor.store.files() == []
    processor.structured.assert_not_called()


def test_daily_csv_filename_controls_outlet_and_reports_python_progress(processor):
    content = ("date,revenue\n" + "2025-08-01,42\n" * 45).encode()
    updates = []
    result = processor.process("daily_summary_AMK-03.csv", content, "csvs", on_progress=lambda value, message: updates.append((value, message)))
    assert result["record_count"] == 45
    processor.structured.assert_not_called()
    cursor = processor.store.repository.return_value._connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    assert all(row[1] == "AMK-03" for row in cursor.executemany.call_args.args[1])
    assert any("Parsed 40/45" in message for _, message in updates)
    assert any("Parsed 45/45" in message for _, message in updates)
    assert [value for value, _ in updates] == sorted(value for value, _ in updates)
    assert updates[0][0] == 0.0
    assert updates[-1][0] == 1.0
    processor.structured.reset_mock()
    assert processor.process("daily_summary_AMK-03.csv", content, "csvs", on_progress=lambda value, message: updates.append((value, message)))["duplicate"]
    processor.structured.assert_not_called()
    assert cursor.execute.call_args.args[0].startswith("DELETE FROM uploaded_outlet_csvs WHERE file_hash=")


def test_text_chunks_keep_original_file_metadata(processor):
    collection = Mock()
    processor.collection = Mock(return_value=collection)
    processor.embeddings = Mock(side_effect=lambda texts: [[0.1, 0.2] for _ in texts])
    result = processor.process("notes.txt", ("Outlet evidence. " * 100).encode(), "texts")
    metadata = collection.upsert.call_args.kwargs["metadatas"]
    assert len(metadata) == result["record_count"]
    assert all(item["source_file"] == result["source_file"] for item in metadata)
    assert all(item["source_type"] == "document" for item in metadata)
    assert all(identifier.startswith("upload-desk:") for identifier in collection.upsert.call_args.kwargs["ids"])
    assert processor.store.files()[0]["kind"] == "texts"


@pytest.mark.parametrize("name,content", [
    ("mail.txt", b"Email export with operational evidence."),
    ("mail.eml", b"Subject: Outlet update\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nOperational email evidence."),
])
def test_email_upload_citations_use_email_folder(processor, name, content):
    collection = Mock()
    processor.collection = Mock(return_value=collection)
    processor.embeddings = Mock(side_effect=lambda texts: [[0.1, 0.2] for _ in texts])
    result = processor.process(name, content, "emails")
    assert result["source_file"] == f"data/docs/emails/{name}"
    assert citation_path(result["source_file"]).read_bytes() == content
    metadata = collection.upsert.call_args.kwargs["metadatas"]
    assert all(item["source_file"] == result["source_file"] and item["source_type"] == "email" for item in metadata)


def test_chroma_opens_existing_shared_collection_without_creating(processor, monkeypatch):
    monkeypatch.setenv("CHROMA_HOST", "shared-chroma")
    with patch("chromadb.HttpClient") as factory:
        processor.collection()
        factory.return_value.get_collection.assert_called_once_with("kaya_kopi_knowledge", embedding_function=None)
        factory.return_value.get_or_create_collection.assert_not_called()


def roster_bytes():
    book = Workbook()
    sheet = book.active
    sheet.title = "Roster Jun 2026"
    sheet.append(["Staff roster June 2026"])
    sheet.append([])
    sheet.append(["Outlet", "Staff Name", "Role", "Mon", "Hourly Rate"])
    sheet.append(["AMK-03", "Example Person", "Barista", "07:00-15:00", 999.25])
    sheet.append(["AMK-03", "Example Person", "Barista", "OFF", 999.25])
    content = io.BytesIO()
    book.save(content)
    book.close()
    return content.getvalue()


def test_roster_parser_preserves_local_days_and_rates_but_excludes_them_from_model_cells():
    sheets = roster_sheets("roster.xlsx", roster_bytes())
    assert sheets[0]["sheet_name"] == "Roster Jun 2026"
    assert [row["source_row_number"] for row in sheets[0]["rows"]] == [4, 5]
    serialized = json.dumps([row["cells"] for row in sheets[0]["rows"]])
    assert "Example Person" not in serialized
    assert "999.25" not in serialized
    assert "Hourly Rate" not in serialized
    assert sheets[0]["rows"][0]["staff_key"] == sheets[0]["rows"][1]["staff_key"]
    fields = sheets[0]["rows"][0]["source_fields"]
    assert fields["mon_shift"] == "07:00-15:00"
    assert fields["tue_shift"] is None
    assert fields["hourly_rate_sgd"] == "999.25"
    assert fields["rate_status"] == "CONFIRMED"
    assert sheets[0]["rows"][1]["source_fields"]["mon_shift"] == "OFF"


@pytest.mark.parametrize("raw,expected,status", [
    (15, "15.00", "CONFIRMED"), ("$14.50/hr", "14.50", "CONFIRMED"),
    ("TBC", "13.00", "CLIENT_APPROVED_DEFAULT"), (None, None, "MISSING"),
    ("", None, "MISSING"), (0, "0.00", "CONFIRMED"),
    ("unknown", None, "REVIEW_REQUIRED"), (-5, None, "REVIEW_REQUIRED"),
    ("NaN", None, "REVIEW_REQUIRED"), ("Infinity", None, "REVIEW_REQUIRED"),
])
def test_roster_rates_preserve_raw_values_and_existing_client_rules(raw, expected, status):
    fields = roster_source_fields(["Hourly Rate"], [raw])
    assert fields["raw_hourly_rate"] == (str(raw).strip() if raw is not None else "")
    assert fields["hourly_rate_sgd"] == expected
    assert fields["rate_status"] == status


def test_roster_preserves_every_weekday_including_off_and_missing():
    fields = roster_source_fields(
        ["Monday", "Tue", "Wed.", "Thursday", "Fri", "Sat", "Sun"],
        ["AM", "PM", "AM/PM", "OFF", None, "07:00-15:00", ""],
    )
    assert [fields[f"{day}_shift"] for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")] == [
        "AM", "PM", "AM/PM", "OFF", None, "07:00-15:00", None,
    ]


def test_stored_individual_rates_are_still_redacted_from_chat_results():
    result = safe_result({"mon_shift": "AM", "hourly_rate_sgd": "15.00", "raw_hourly_rate": "$15/hr", "rate_status": "CONFIRMED"})
    assert result["hourly_rate_sgd"] == "[REDACTED]"
    assert result["raw_hourly_rate"] == "[REDACTED]"
    assert result["mon_shift"] == "AM"


def test_excel_roster_saves_month_keys_and_original_citation(processor):
    processor.structured.return_value = RosterBatch(rows=[RosterRow(
        source_row_number=number, is_assignment=True, roster_month="2026-06-01",
        shift_date=None, outlet_name="AMK-03", role="Barista", shift_window="Mon 07:00-15:00",
        paid_hours=None, notes=None,
    ) for number in (4, 5)])
    result = processor.process("roster.xlsx", roster_bytes(), "rosters")
    assert result["source_file"] == "data/tabular/roster.xlsx"
    assert result["record_count"] == 2
    cursor = processor.store.repository.return_value._connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    values = cursor.executemany.call_args.args[1]
    assert values[0][1:4] == ("Roster Jun 2026", 4, "2026-06-01")
    assert values[0][6] == values[1][6]
    assert values[0][-1] == "data/tabular/roster.xlsx"
    assert values[0][11:18] == ("07:00-15:00", None, None, None, None, None, None)
    assert values[0][18:21] == ("999.25", "999.25", "CONFIRMED")
    assert "999.25" not in processor.structured.call_args.args[3]


def test_existing_roster_backfills_days_and_rates_without_openai(processor):
    content = roster_bytes()
    import hashlib
    digest = hashlib.sha256(content).hexdigest()
    processor.store.record(digest, "data/tabular/roster.xlsx", "roster.xlsx", "rosters", 2)
    result = processor.process("roster.xlsx", content, "rosters")
    assert result["duplicate"]
    processor.structured.assert_not_called()
    cursor = processor.store.repository.return_value._connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    sql, rows = cursor.executemany.call_args.args
    assert "UPDATE uploaded_roster_rows" in sql
    assert "source_row_number=%s" in sql
    assert rows[0][:10] == ("07:00-15:00", None, None, None, None, None, None, "999.25", "999.25", "CONFIRMED")
    assert rows[0][-3:] == (digest, "Roster Jun 2026", 4)


def test_roster_column_migration_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("KAYA_PROJECT_ROOT", str(tmp_path))
    store = UploadStore(Settings(tmp_path / "data" / "upload_assistant"))
    repository = MagicMock()
    store.repository = Mock(return_value=repository)
    cursor = repository._connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{"Field": column} for column in {*ROSTER_SOURCE_COLUMNS, *CSV_SOURCE_COLUMNS}]
    store.init_mysql()
    assert not any("ALTER TABLE" in call.args[0] for call in cursor.execute.call_args_list)


def test_excel_roster_rejects_mismatched_row_locations(processor):
    processor.structured.return_value = RosterBatch(rows=[])
    with pytest.raises(ValueError, match="locations changed"):
        processor.process("roster.xlsx", roster_bytes(), "rosters")
    processor.store.init_mysql.assert_not_called()


def model_message(content="", calls=None):
    message = Mock()
    message.content = content
    message.tool_calls = calls or []
    message.model_dump.return_value = {"role": "assistant", "content": content, "tool_calls": [
        {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}} for call in message.tool_calls
    ]}
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def tool_call(name, sql, identifier):
    return SimpleNamespace(id=identifier, type="function", function=SimpleNamespace(name=name, arguments=json.dumps({"sql": sql})))


def test_chat_fetches_provenance_after_aggregate_and_returns_structured_sources(processor):
    _, source = processor.store.archive("invoice.png", b"original", "invoices")
    processor.store.query = Mock(side_effect=[
        {"ok": True, "rows": [{"total": 42}]},
        {"ok": True, "rows": [{"source_file": source}]},
    ])
    processor.client.chat.completions.create.side_effect = [
        model_message(calls=[tool_call("query_uploads", "SELECT SUM(total_amount) FROM uploaded_invoices", "total")]),
        model_message(calls=[tool_call("query_uploads", "SELECT source_file FROM uploaded_invoices", "sources")]),
        model_message("Observed supplier invoices total SGD 42."),
    ]
    agent = UploadAgent(processor)
    agent.existing_schema = Mock(return_value={})
    result = agent.ask([{"role": "user", "content": "How much did invoices total?"}])
    assert result["sources"] == [source]
    assert "SGD 42" in result["content"]
    assert processor.client.chat.completions.create.call_args_list[1].kwargs["tool_choice"] == "required"
    assert len(result["tools"]) == 2


def test_chat_never_uses_failed_query_as_evidence(processor):
    processor.store.query = Mock(return_value={"ok": False, "error": "Unavailable"})
    processor.client.chat.completions.create.side_effect = [
        model_message(calls=[tool_call("query_uploads", "SELECT * FROM uploaded_invoices", "failed")]),
        model_message("The total is 999."),
    ]
    agent = UploadAgent(processor)
    agent.existing_schema = Mock(return_value={})
    result = agent.ask([{"role": "user", "content": "Invoice total?"}])
    assert "999" not in result["content"]
    assert result["sources"] == []


@pytest.mark.parametrize("question", [
    "what's our margin on kopi at malls outlets this quarter",
    "What is the gross profit by outlet?",
])
def test_margin_questions_explain_missing_inputs_without_tool_loop(processor, question):
    agent = UploadAgent(processor)
    result = agent.ask([{"role": "user", "content": question}])
    content = result["content"].lower()
    assert "can't verify a margin" in content
    assert "item-level cost of goods sold" in content
    assert "invented figure" in content
    assert result["sources"] == []
    assert processor.client.chat.completions.create.call_count == 0


def test_step_limit_falls_back_to_answer_from_existing_evidence(processor):
    _, source = processor.store.archive("invoice.png", b"original", "invoices")
    processor.store.query = Mock(return_value={"ok": True, "rows": [{"source_file": source}]})
    processor.client.chat.completions.create.side_effect = [
        model_message(calls=[tool_call("query_uploads", "SELECT source_file FROM uploaded_invoices", f"step-{index}")])
        for index in range(8)
    ] + [model_message("Observed invoice files were returned.")]
    agent = UploadAgent(processor)
    agent.existing_schema = Mock(return_value={})
    result = agent.ask([{"role": "user", "content": "List invoice evidence"}])
    assert "Observed invoice files were returned." in result["content"]
    assert result["sources"] == [source]
    assert processor.client.chat.completions.create.call_count == 9
    assert processor.client.chat.completions.create.call_args_list[-1].kwargs["tool_choice"] == "none"


@pytest.mark.parametrize("configured,expected", [("gpt-4o", "gpt-4o"), (None, "gpt-4o-mini")])
def test_chat_uses_only_openai_chat_model(processor, monkeypatch, configured, expected):
    monkeypatch.setenv("UPLOAD_AI_CHAT_MODEL", "unused-legacy-model")
    if configured is None:
        monkeypatch.delenv("OPENAI_CHAT_MODEL", raising=False)
    else:
        monkeypatch.setenv("OPENAI_CHAT_MODEL", configured)
    processor.client.chat.completions.create.return_value = model_message("No verified data.")
    agent = UploadAgent(processor)
    agent.existing_schema = Mock(return_value={})
    agent.ask([{"role": "user", "content": "Check invoice records"}])
    assert processor.client.chat.completions.create.call_args.kwargs["model"] == expected
    assert processor.log.call_args.args[2] == expected


def usage_response(input_tokens, output_tokens):
    return SimpleNamespace(
        model="gpt-4o-mini", id="response-test", service_tier="default",
        usage=SimpleNamespace(prompt_tokens=input_tokens, completion_tokens=output_tokens,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=0, audio_tokens=0),
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=0, audio_tokens=0)),
    )


def test_usage_groups_all_calls_and_embeddings_by_saved_turn(processor):
    processor.budget = BudgetGuard(processor.store.settings.root / "test_usage.sqlite3", 50)
    processor.client.embeddings.create.return_value = SimpleNamespace(
        model="text-embedding-3-small", usage=SimpleNamespace(prompt_tokens=20),
        data=[SimpleNamespace(index=0, embedding=[0.1, 0.2])],
    )
    with processor.track_turn("conversation-1", "turn-1", "Find invoice test@example.com"):
        UploadProcessor.log(processor, usage_response(100, 10), "chat", "gpt-4o-mini")
        UploadProcessor.log(processor, usage_response(50, 5), "chat", "gpt-4o-mini")
        processor.embeddings(["invoice"])
    with processor.track_turn("conversation-2", "turn-2", "Another question"):
        UploadProcessor.log(processor, usage_response(10, 1), "chat", "gpt-4o-mini")
    summary = processor.budget.usage_tracker.get_turn_usage("conversation-1", "turn-1")
    assert summary["input_tokens"] == 170
    assert summary["output_tokens"] == 15
    assert summary["total_tokens"] == 185
    assert summary["total_cost"] > 0
    assert summary["models"] == ["gpt-4o-mini", "text-embedding-3-small"]
    assert all("test@example.com" not in row["prompt"] for row in processor.budget.usage_tracker.get_all_usage_history())
    assert processor.budget.usage_tracker.get_turn_usage("conversation-1", "turn-2") is None
    UploadProcessor.log(processor, usage_response(1, 1), "invoice-ocr", "gpt-4o-mini")
    assert processor.budget.usage_tracker.get_all_usage_history()[0]["turn_id"] is None


def test_missing_usage_is_reported_as_unavailable_not_zero(processor):
    processor.budget = BudgetGuard(processor.store.settings.root / "missing_usage.sqlite3", 50)
    response = usage_response(100, 10)
    response.usage = None
    with processor.track_turn("conversation", "missing-turn", "Check records"):
        UploadProcessor.log(processor, response, "chat", "gpt-4o-mini")
    summary = processor.budget.usage_tracker.get_turn_usage("conversation", "missing-turn")
    assert summary["total_tokens"] is None
    assert summary["total_cost"] is None


def test_fresh_browser_restores_latest_saved_conversation(processor, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    processor.store.files = Mock(return_value=[])
    processor.budget.get_current_month_spend.return_value = 0.0
    processor.budget.usage_tracker.get_all_usage_history.return_value = []
    processor.budget.usage_tracker.get_unpriced_count.return_value = 0
    processor.store.save_conversation("older", [{"role": "user", "content": "Older question"}])
    processor.store.save_conversation("latest", [{"role": "user", "content": "Latest question"}])
    agent = Mock()
    monkeypatch.setattr("kaya_kopi.upload_assistant.storage.UploadStore", Mock(return_value=processor.store))
    monkeypatch.setattr("kaya_kopi.upload_assistant.ingestion.UploadProcessor", Mock(return_value=processor))
    monkeypatch.setattr("kaya_kopi.upload_assistant.agent.UploadAgent", Mock(return_value=agent))
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/kaya_kopi/upload_assistant/app.py")).run()
        assert app.session_state["upload_desk_thread_id"] == "latest"
        assert app.session_state["upload_desk_messages"][-1]["content"] == "Latest question"
        next(item for item in app.button if item.label == "New conversation").click().run()
        assert app.session_state["upload_desk_thread_id"] != "latest"
        assert app.session_state["upload_desk_messages"] == []
        agent.ask.assert_not_called()
    finally:
        st.cache_resource.clear()


def test_saved_chat_ui_reopens_renames_deletes_and_preserves_usage(processor, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    processor.store.files = Mock(return_value=[])
    processor.budget = BudgetGuard(processor.store.settings.root / "ui_usage.sqlite3", 50)
    agent = Mock()

    def answer(history, *, thread_id, turn_id):
        with processor.track_turn(thread_id, turn_id, history[-1]["content"]):
            UploadProcessor.log(processor, usage_response(100, 10), "chat", "gpt-4o-mini")
            UploadProcessor.log(processor, usage_response(50, 5), "chat", "gpt-4o-mini")
        return {"content": "Observed total is SGD 42.", "sources": [], "tools": [{"name": "query_uploads", "ok": True}]}

    agent.ask.side_effect = answer
    monkeypatch.setattr("kaya_kopi.upload_assistant.storage.UploadStore", Mock(return_value=processor.store))
    monkeypatch.setattr("kaya_kopi.upload_assistant.ingestion.UploadProcessor", Mock(return_value=processor))
    monkeypatch.setattr("kaya_kopi.upload_assistant.agent.UploadAgent", Mock(return_value=agent))
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/kaya_kopi/upload_assistant/app.py")).run()
        assert [tab.label for tab in app.tabs] == ["Assistant", "Upload", "Usage Log", "Files"]
        app.chat_input[0].set_value("Check invoice total").run()
        assert not app.exception
        assert agent.ask.call_count == 1
        thread = processor.store.list_conversations()[0]
        thread_id = thread["thread_id"]
        assert any("Tokens: 165" in item.value and "Estimated cost:" in item.value for item in app.caption)
        assert len(processor.budget.usage_tracker.get_all_usage_history()) == 2
        assert any(item.label == "Known estimated USD" for item in app.metric)
        next(item for item in app.text_input if item.key == f"rename_{thread_id}").set_value("Invoice totals")
        next(item for item in app.button if item.key == f"save_{thread_id}").click().run()
        assert processor.store.list_conversations()[0]["name"] == "Invoice totals"
        next(item for item in app.button if item.label == "New conversation").click().run()
        assert app.session_state["upload_desk_messages"] == []
        next(item for item in app.button if item.key == f"open_{thread_id}").click().run()
        assert app.session_state["upload_desk_messages"][-1]["content"] == "Observed total is SGD 42."
        assert "Tokens: 165" in app.session_state["upload_desk_messages"][-1]["usage_caption"]
        next(item for item in app.checkbox if item.key == f"confirm_{thread_id}").check().run()
        next(item for item in app.button if item.key == f"delete_{thread_id}").click().run()
        assert not app.exception
        assert processor.store.list_conversations() == []
        assert len(processor.budget.usage_tracker.get_all_usage_history()) == 2
        assert agent.ask.call_count == 1
    finally:
        st.cache_resource.clear()


def test_copilot_history_is_found_from_a_different_working_directory(tmp_path, monkeypatch):
    from kaya_kopi.upload_assistant import copilot_bridge

    monkeypatch.setenv("KAYA_PROJECT_ROOT", str(tmp_path))
    copilot_db = tmp_path / "src" / "kaya_kopi" / "copilot" / ".kaya_chat.sqlite3"
    monkeypatch.setenv("KAYA_CHAT_DB", str(copilot_db))
    copilot_bridge.save_thread("copilot-history", "Old chat", [
        {"role": "user", "content": "Compare sales", "turn_id": "old-1"},
        {"role": "assistant", "content": "Sales were compared."},
    ])

    # Simulate running Upload Desk from the repository root while the copilot
    # history was created by `streamlit_app.py` in its own directory.
    monkeypatch.delenv("KAYA_CHAT_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert copilot_bridge.chat_db_path() == copilot_db
    assert copilot_bridge.list_threads() == [
        {"thread_id": "copilot-history", "name": "Old chat"}
    ]
    assert [item["content"] for item in copilot_bridge.load_messages("copilot-history")] == [
        "Compare sales", "Sales were compared.",
    ]


def test_chat_db_falls_back_when_configured_path_cannot_be_opened(tmp_path, monkeypatch):
    from kaya_kopi.upload_assistant import copilot_bridge

    monkeypatch.setenv("KAYA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory")
    monkeypatch.setenv("KAYA_CHAT_DB", str(blocked_parent / "chat.sqlite3"))

    copilot_bridge.save_thread("fallback", "Fallback chat", [
        {"role": "user", "content": "Keep working", "turn_id": "fallback-1"},
        {"role": "assistant", "content": "Saved to a writable database."},
    ])

    assert copilot_bridge.chat_db_path() != blocked_parent / "chat.sqlite3"
    assert copilot_bridge.list_threads() == [
        {"thread_id": "fallback", "name": "Fallback chat"}
    ]


def test_existing_data_tools_discover_and_query_local_sqlite(tmp_path, monkeypatch):
    import sqlite3
    from kaya_kopi.upload_assistant import existing_data_tools

    database = tmp_path / "kaya_ops.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE allowed (id INTEGER, name TEXT)")
    connection.execute("INSERT INTO allowed VALUES (1, 'Alpha')")
    connection.commit()
    connection.close()
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(database))
    monkeypatch.setenv("KAYA_LLM_SQLITE_TABLES", "allowed")

    discovered = existing_data_tools.discover_database_schema.invoke({"database": "sqlite"})
    assert discovered["ok"]
    assert discovered["schema"] == {"allowed": ["id", "name"]}

    queried = existing_data_tools.query_database.invoke({
        "database": "sqlite", "sql": "SELECT id, name FROM allowed", "limit": 10,
    })
    assert queried["ok"]
    assert queried["rows"] == [{"id": 1, "name": "Alpha"}]


def test_upload_chat_is_written_in_copilot_checkpoint_format(processor, monkeypatch):
    from kaya_kopi.upload_assistant import copilot_bridge

    messages = [
        {"role": "user", "content": "Check invoice total", "turn_id": "turn-1"},
        {"role": "assistant", "content": "Observed total is SGD 42."},
    ]
    copilot_bridge.save_thread("shared-thread", "Invoice totals", messages)

    assert copilot_bridge.list_threads() == [
        {"thread_id": "shared-thread", "name": "Invoice totals"}
    ]
    assert [item["content"] for item in copilot_bridge.load_messages("shared-thread")] == [
        "Check invoice total", "Observed total is SGD 42.",
    ]

    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(processor.store.settings.root / "usage.sqlite3"))
    from streamlit.testing.v1 import AppTest

    from kaya_kopi.copilot import chat_backend

    chat_backend.get_chatbot.cache_clear()
    try:
        # The unmodified copilot backend lists and opens the Upload Desk chat.
        assert chat_backend.retrieve_all_threads() == ["shared-thread"]
        assert chat_backend.get_thread_names() == {"shared-thread": "Invoice totals"}
        state = chat_backend.get_chatbot().get_state(
            {"configurable": {"thread_id": "shared-thread"}}
        )
        assert [message.content for message in state.values["messages"]] == [
            "Check invoice total", "Observed total is SGD 42.",
        ]

        app_path = Path(__file__).resolve().parents[2] / "src/kaya_kopi/copilot/streamlit_app.py"
        app = AppTest.from_file(str(app_path), default_timeout=30).run()
        app.button(key="shared-thread").click().run()
        assert not app.exception
        assert [item["content"] for item in app.session_state["message_history"]] == [
            "Check invoice total", "Observed total is SGD 42.",
        ]
    finally:
        chat_backend.get_chatbot.cache_clear()


def test_copilot_chat_is_listed_and_opened_in_upload_desk(processor, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    from kaya_kopi.upload_assistant import copilot_bridge

    copilot_bridge.save_thread("copilot-thread", "Ops question", [
        {"role": "user", "content": "Compare outlet sales", "turn_id": "turn-2"},
        {"role": "assistant", "content": "Here is the comparison."},
    ])
    processor.store.files = Mock(return_value=[])
    processor.budget.get_current_month_spend.return_value = 0.0
    processor.budget.usage_tracker.get_all_usage_history.return_value = []
    processor.budget.usage_tracker.get_unpriced_count.return_value = 0
    monkeypatch.setattr("kaya_kopi.upload_assistant.storage.UploadStore", Mock(return_value=processor.store))
    monkeypatch.setattr("kaya_kopi.upload_assistant.ingestion.UploadProcessor", Mock(return_value=processor))
    monkeypatch.setattr("kaya_kopi.upload_assistant.agent.UploadAgent", Mock(return_value=Mock()))
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/kaya_kopi/upload_assistant/app.py")).run()
        next(item for item in app.button if item.key == "open_copilot-thread").click().run()
        assert not app.exception
        assert [item["content"] for item in app.session_state["upload_desk_messages"]] == [
            "Compare outlet sales", "Here is the comparison.",
        ]
        next(item for item in app.checkbox if item.key == "confirm_copilot-thread").check().run()
        next(item for item in app.button if item.key == "delete_copilot-thread").click().run()
        assert not app.exception
        assert copilot_bridge.list_threads() == []
        assert processor.store.list_conversations() == []
        app.run()
        assert copilot_bridge.list_threads() == [], "deleted chat must not be resurrected"
    finally:
        st.cache_resource.clear()


def test_strict_mysql_aggregate_error_explains_how_to_recover(processor):
    processor.store.query = Mock(side_effect=pymysql.OperationalError(1140, "Nonaggregated column source_file is incompatible with GROUP BY"))
    result = UploadAgent(processor).dispatch("query_uploads", {"sql": "SELECT COUNT(*),source_file FROM uploaded_roster_rows"})
    assert result["ok"] is False
    assert result["sql_error_code"] == 1140
    assert "Nonaggregated column" in result["error"]
    assert "separately" in result["recovery"]


def test_empty_successful_lookup_is_not_reported_as_connection_failure(processor):
    processor.store.query = Mock(return_value={"ok": True, "rows": []})
    processor.client.chat.completions.create.side_effect = [
        model_message(calls=[tool_call("query_uploads", "SELECT role FROM uploaded_roster_rows", "empty")]),
        model_message("No matching roster records were returned; this does not establish zero attendance."),
    ]
    agent = UploadAgent(processor)
    agent.existing_schema = Mock(return_value={})
    result = agent.ask([{"role": "user", "content": "Check roster records"}])
    assert "No matching roster records" in result["content"]
    assert "Check the upload inventory" not in result["content"]


@pytest.mark.parametrize("table,sql", [
    ("outlets", "SELECT outlet_code, outlet_name FROM outlets"),
    ("pos_transactions", "SELECT tx_time, total FROM pos_transactions"),
    ("supplier", "SELECT canonical_name FROM supplier"),
    ("menu_items", "SELECT item_name, base_price FROM menu_items"),
])
def test_client_tables_use_existing_mysql_query_tool(processor, monkeypatch, table, sql):
    monkeypatch.setenv("UPLOAD_AI_EXISTING_DATA", "true")
    with patch("kaya_kopi.upload_assistant.agent.query_database") as query:
        query.invoke.return_value = {"ok": True, "rows": []}
        result = UploadAgent(processor).dispatch("query_existing", {"database": "mysql", "sql": sql})
        assert result["ok"]
        query.invoke.assert_called_once_with({"database": "mysql", "sql": sql, "limit": 100})
        processor.store.repository.assert_not_called()


@pytest.mark.parametrize("table", ["outlets", "pos_transactions", "supplier", "menu_items"])
def test_client_table_guidance_is_injected_only_for_discovered_tables(processor, table):
    agent = UploadAgent(processor)
    agent.existing_schema = Mock(return_value={"mysql": {table: []}})
    processor.client.chat.completions.create.return_value = model_message("No verified result.")
    agent.ask([{"role": "user", "content": "Check client database records"}])
    request = processor.client.chat.completions.create.call_args.kwargs
    guidance = json.loads(request["messages"][0]["content"].split("Client MySQL table guidance:\n", 1)[1])
    assert set(guidance) == {table}
    assert guidance[table]["purpose"]
    assert any(tool["function"]["name"] == "query_existing" for tool in request["tools"])


def test_existing_client_access_can_still_be_disabled(processor, monkeypatch):
    monkeypatch.setenv("UPLOAD_AI_EXISTING_DATA", "false")
    with patch("kaya_kopi.upload_assistant.agent.query_database") as query:
        result = UploadAgent(processor).dispatch("query_existing", {"database": "mysql", "sql": "SELECT * FROM outlets"})
        assert result["ok"] is False
        query.invoke.assert_not_called()


@pytest.mark.parametrize("question,code", [
    ("when AMK-03 change name?", "AMK-03"),
    ("any history about outlet AMK-3A changes?", "AMK-3A"),
    ("when outlet AMK-03 was re-registered as AMK-3A?", "AMK-03"),
])
def test_outlet_history_phrasings_use_same_citable_lookup(processor, monkeypatch, question, code):
    monkeypatch.setenv("UPLOAD_AI_EXISTING_DATA", "true")
    monkeypatch.setenv("KAYA_LLM_MYSQL_TABLES", "outlet_alias,outlets,source_document")
    _, source = processor.store.archive("outlet_alias_note.txt", b"Outlet registration evidence", "texts")
    processor.store.repository.return_value._query.return_value = [{
        "canonical_outlet_code": "AMK-03", "alias_code": "AMK-3A",
        "effective_from": "2026-01-01", "effective_to": None,
        "validation_status": "VERIFIED", "source_uri": source,
        "evidence_text": "Re-registration evidence from source document",
    }]
    call = SimpleNamespace(id="history", type="function", function=SimpleNamespace(name="lookup_outlet_history", arguments=json.dumps({"outlet_code": code})))
    processor.client.chat.completions.create.side_effect = [
        model_message(calls=[call]), model_message("Verified re-registration effective 2026-01-01; no end date is recorded."),
    ]
    agent = UploadAgent(processor)
    agent.existing_schema = Mock(return_value={"mysql": {"outlet_alias": [], "outlets": [], "source_document": []}})
    result = agent.ask([{"role": "user", "content": question}])
    assert history_outlet(question) == code
    assert result["sources"] == [source]
    assert "provenance was not returned" not in result["content"]
    assert processor.client.chat.completions.create.call_args_list[0].kwargs["tool_choice"] == {"type": "function", "function": {"name": "lookup_outlet_history"}}
    query = processor.store.repository.return_value._query.call_args
    assert "d.source_document_id=a.evidence_document_id" in query.args[0]
    assert query.args[1] == (code, code, code)


def test_outlet_history_cannot_bypass_table_allowlist(processor, monkeypatch):
    monkeypatch.setenv("KAYA_LLM_MYSQL_TABLES", "outlet_alias,outlets")
    result = UploadAgent(processor).dispatch("lookup_outlet_history", {"outlet_code": "AMK-03"})
    assert result["ok"] is False
    processor.store.repository.assert_not_called()


def test_source_warning_is_shown_once_and_removed_when_sources_exist():
    answer = "Alias recorded.\nNo original file provenance was returned for this answer.\nOriginal-file provenance was not returned for this answer."
    assert with_source_warning(answer, False).count("provenance") == 1
    assert with_source_warning(answer, True) == "Alias recorded."


def test_price_change_question_is_not_routed_to_outlet_history():
    assert history_outlet("Can we change the coffee price at AMK-03?") is None


def test_streamlit_csv_processing_shows_progress_without_real_services(processor, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    source = processor.store.settings.root.parent / "tabular" / "daily_summary_AMK-03.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"date,revenue\n2025-08-01,42\n")
    processor.store.files = Mock(return_value=[])
    processor.budget.get_current_month_spend.return_value = 0.0
    monkeypatch.setattr("kaya_kopi.upload_assistant.storage.UploadStore", Mock(return_value=processor.store))
    monkeypatch.setattr("kaya_kopi.upload_assistant.ingestion.UploadProcessor", Mock(return_value=processor))
    monkeypatch.setattr("kaya_kopi.upload_assistant.agent.UploadAgent", Mock())
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/kaya_kopi/upload_assistant/app.py")).run()
        assert [tab.label for tab in app.tabs] == ["Assistant", "Upload", "Usage Log", "Files"]
        assert len(app.chat_input) == 1
        assert not app.tabs[0].get("chat_input")
        assert app.chat_input[0].key == "upload_desk_chat"
        styles = next(item.value for item in app.markdown if "<style>" in item.value)
        assert 'position: sticky' in styles
        assert '[role="tablist"]' in styles
        assert '[data-testid="stBottom"]' in styles
        next(item for item in app.selectbox if item.label == "Document type").select("Outlet CSVs").run()
        next(item for item in app.multiselect if item.label == "Existing data files").select("data/tabular/daily_summary_AMK-03.csv").run()
        next(item for item in app.button if item.label == "Process selected files").click().run()
        assert not app.exception
        assert app.get("progress")[0].proto.value == 100
        assert "Complete: 1 records saved." in app.get("progress")[0].proto.text
        assert any("1 records" in item.value for item in app.success)
        processor.client.chat.completions.create.assert_not_called()
    finally:
        st.cache_resource.clear()