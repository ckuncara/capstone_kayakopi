from dataclasses import dataclass, field
from typing import Literal


@dataclass
class TextChunk:
    chunk_id: str
    text: str
    metadata: dict = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def chunk_single(
    doc_id: str,
    text: str,
    base_metadata: dict | None = None,
) -> list[TextChunk]:
    """Keeps the entire document in 1 single chunk (used for supplier invoices)."""
    meta = {
        **(base_metadata or {}),
        "chunk_index": 0,
        "total_chunks": 1,
        "token_count": estimate_tokens(text),
    }
    return [TextChunk(chunk_id=doc_id, text=text, metadata=meta)]


def chunk_by_exact_count(
    doc_id: str,
    text: str,
    num_chunks: int,
    overlap_chars: int = 100,
    base_metadata: dict | None = None,
) -> list[TextChunk]:
    """Splits text into an exact target number of chunks (N chunks)."""
    cleaned = text.strip()
    if not cleaned:
        return []
    if num_chunks <= 1 or len(cleaned) <= num_chunks:
        return chunk_single(doc_id, cleaned, base_metadata)

    step = len(cleaned) // num_chunks
    chunks: list[TextChunk] = []

    for i in range(num_chunks):
        start = max(0, i * step - (overlap_chars if i > 0 else 0))
        end = len(cleaned) if i == num_chunks - 1 else min(len(cleaned), (i + 1) * step)
        segment = cleaned[start:end].strip()
        if not segment:
            continue
        meta = {
            **(base_metadata or {}),
            "chunk_index": i,
            "total_chunks": num_chunks,
            "token_count": estimate_tokens(segment),
        }
        chunks.append(TextChunk(chunk_id=f"{doc_id}::chunk_{i}", text=segment, metadata=meta))

    return chunks


def chunk_by_size(
    doc_id: str,
    text: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 150,
    base_metadata: dict | None = None,
) -> list[TextChunk]:
    """Splits long text documents (emails, WhatsApp exports, SOPs) by character window."""
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= chunk_size:
        return chunk_single(doc_id, cleaned, base_metadata)

    segments: list[str] = []
    start = 0
    while start < len(cleaned):
        end = min(len(cleaned), start + chunk_size)
        segments.append(cleaned[start:end].strip())
        if end == len(cleaned):
            break
        start = max(start + 1, end - chunk_overlap)

    total = len(segments)
    return [
        TextChunk(
            chunk_id=f"{doc_id}::chunk_{i}",
            text=seg,
            metadata={
                **(base_metadata or {}),
                "chunk_index": i,
                "total_chunks": total,
                "token_count": estimate_tokens(seg),
            },
        )
        for i, seg in enumerate(segments)
        if seg
    ]


def chunk_rows(
    doc_id: str,
    row_descriptions: list[str],
    rows_per_chunk: int = 1,
    base_metadata: dict | None = None,
) -> list[TextChunk]:
    """Groups tabular CSV/Excel row-to-sentence descriptions into N rows per chunk."""
    clean_rows = [r.strip() for r in row_descriptions if r.strip()]
    if not clean_rows:
        return []

    grouped = [
        "\n".join(clean_rows[i : i + rows_per_chunk])
        for i in range(0, len(clean_rows), max(1, rows_per_chunk))
    ]
    total = len(grouped)
    return [
        TextChunk(
            chunk_id=f"{doc_id}::rowchunk_{i}",
            text=grp,
            metadata={
                **(base_metadata or {}),
                "chunk_index": i,
                "total_chunks": total,
                "token_count": estimate_tokens(grp),
            },
        )
        for i, grp in enumerate(grouped)
    ]


def build_chunks(
    doc_id: str,
    text: str = "",
    strategy: Literal["single", "exact_count", "by_size", "rows"] = "single",
    num_chunks: int = 1,
    chunk_size: int = 1000,
    chunk_overlap: int = 150,
    row_descriptions: list[str] | None = None,
    rows_per_chunk: int = 1,
    base_metadata: dict | None = None,
) -> list[TextChunk]:
    if strategy == "single":
        return chunk_single(doc_id, text, base_metadata)
    if strategy == "exact_count":
        return chunk_by_exact_count(doc_id, text, num_chunks, chunk_overlap, base_metadata)
    if strategy == "by_size":
        return chunk_by_size(doc_id, text, chunk_size, chunk_overlap, base_metadata)
    if strategy == "rows":
        return chunk_rows(doc_id, row_descriptions or [], rows_per_chunk, base_metadata)
    raise ValueError(f"Unsupported chunking strategy: {strategy}")