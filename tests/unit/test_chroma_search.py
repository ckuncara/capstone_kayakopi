import chromadb
from chromadb.api.types import EmbeddingFunction
import pytest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from kaya_kopi.config import AppConfig
from kaya_kopi.ingestion.parsers.vector_chunking import build_chunks
from kaya_kopi.repositories.chroma_store import SharedChromaRepository
from kaya_kopi.security.budget_guard import BudgetGuard


class FixedEmbedding(EmbeddingFunction):
    def __init__(self):
        pass

    def __call__(self, input):
        return [[1.0, 0.0] for _ in input]

    @staticmethod
    def name():
        return "fixed_test_embedding"


@pytest.mark.parametrize(
    ("branch_code", "source_type", "expected"),
    [
        ("HQ", "invoice", {"hq-invoice", "branch-invoice"}),
        ("amk-01", "invoice", {"branch-invoice"}),
        ("amk-01", None, {"branch-invoice", "branch-guide"}),
        (None, None, {"hq-invoice", "branch-invoice", "branch-guide"}),
        ("missing", "invoice", set()),
    ],
)
def test_semantic_search_filters_and_returns_flat_results(
    monkeypatch, branch_code, source_type, expected
):
    collection = chromadb.EphemeralClient().create_collection(
        f"test_invoice_search_{uuid4().hex}", embedding_function=FixedEmbedding()
    )
    collection.add(
        ids=["hq-invoice", "branch-invoice", "branch-guide"],
        documents=["hq-invoice", "branch-invoice", "branch-guide"],
        metadatas=[
            {"source_type": "invoice", "branch_code": "HQ"},
            {"source_type": "invoice", "branch_code": "AMK-01"},
            {"source_type": "guide", "branch_code": "AMK-01"},
        ],
    )
    monkeypatch.setattr(SharedChromaRepository, "get_collection", lambda self: collection)
    repo = object.__new__(SharedChromaRepository)
    repo.budget_guard = None

    hits = repo.semantic_search("question", branch_code, source_type, n_results=4)

    assert {document for _, document in hits} == expected
    assert all(isinstance(metadata, dict) for metadata, _ in hits)


def test_search_records_actual_embedding_usage(tmp_path, monkeypatch):
    cfg = AppConfig(sqlite_db_path=tmp_path / "usage.sqlite3")
    guard = BudgetGuard(cfg.sqlite_db_path)
    repo = object.__new__(SharedChromaRepository)
    repo.cfg = cfg
    repo.budget_guard = guard
    repo.embed_fn = Mock()
    repo.embed_fn.client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[1.0, 0.0])],
        usage=SimpleNamespace(prompt_tokens=42), model=cfg.embed_model,
    )
    collection = Mock()
    collection.count.return_value = 1
    collection.query.return_value = {"metadatas": [[{"source_type": "invoice"}]], "documents": [["result"]]}
    monkeypatch.setattr(repo, "get_collection", lambda: collection)

    assert repo.semantic_search("private question") == [({"source_type": "invoice"}, "result")]
    assert collection.query.call_args.kwargs["query_embeddings"] == [[1.0, 0.0]]
    usage = guard.usage_tracker.get_latest_usage()
    assert usage["thread_id"] == "embedding:search"
    assert usage["prompt"] == "Embed search query"
    assert usage["input_tokens"] == 42
    assert usage["output_tokens"] == 0

    collection.query.side_effect = RuntimeError("Chroma unavailable")
    with pytest.raises(RuntimeError, match="Chroma unavailable"):
        repo.semantic_search("another private question")
    assert len(guard.usage_tracker.get_all_usage_history()) == 2


def test_actual_embedding_vectors_work_with_chroma(tmp_path, monkeypatch):
    cfg = AppConfig(sqlite_db_path=tmp_path / "usage.sqlite3")
    guard = BudgetGuard(cfg.sqlite_db_path)
    repo = object.__new__(SharedChromaRepository)
    repo.cfg = cfg
    repo.budget_guard = guard
    repo.embed_fn = Mock()
    repo.embed_fn.client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[1.0, 0.0])],
        usage=SimpleNamespace(prompt_tokens=42), model=cfg.embed_model,
    )
    collection = chromadb.EphemeralClient().create_collection(
        f"usage_test_{uuid4().hex}", embedding_function=FixedEmbedding()
    )
    monkeypatch.setattr(repo, "get_collection", lambda: collection)
    chunks = build_chunks("doc1", text="Coffee invoice", strategy="single")

    assert repo.upsert_chunks("doc1", chunks, "invoice") == 1
    assert repo.semantic_search("coffee")[0][1] == "Coffee invoice"
    assert len(guard.usage_tracker.get_all_usage_history()) == 2
    assert repo.embed_fn.client.embeddings.create.call_count == 2
