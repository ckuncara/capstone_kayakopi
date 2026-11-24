import chromadb
from chromadb.api.types import EmbeddingFunction
import pytest
from uuid import uuid4

from kaya_kopi.repositories.chroma_store import SharedChromaRepository


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

    hits = repo.semantic_search("question", branch_code, source_type, n_results=4)

    assert {document for _, document in hits} == expected
    assert all(isinstance(metadata, dict) for metadata, _ in hits)
