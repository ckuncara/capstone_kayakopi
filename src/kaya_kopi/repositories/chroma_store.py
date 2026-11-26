import chromadb
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
from kaya_kopi.config import AppConfig
from kaya_kopi.ingestion.parsers.vector_chunking import TextChunk, build_chunks, estimate_tokens
from kaya_kopi.security.budget_guard import BudgetGuard
from kaya_kopi.security.openai_proxy import openai_client_options
from kaya_kopi.security.pdpa_scrubber import scrub_pdpa


class SharedChromaRepository:
    def __init__(
        self,
        cfg: AppConfig,
        collection_name: str | None = None,
        budget_guard: BudgetGuard | None = None,
    ):
        self.cfg = cfg
        self.collection_name = collection_name or cfg.chroma_collection
        self.budget_guard = budget_guard or BudgetGuard(cfg.sqlite_db_path, cfg.monthly_budget_usd)

        if cfg.chroma_host:
            self.client = chromadb.HttpClient(host=cfg.chroma_host, port=cfg.chroma_port)
        else:
            cfg.chroma_dir.mkdir(parents=True, exist_ok=True)
            self.client = chromadb.PersistentClient(path=str(cfg.chroma_dir))

        client_options = openai_client_options(cfg, "embedding")
        self.embed_fn = OpenAIEmbeddingFunction(
            api_key=client_options["api_key"],
            model_name=cfg.embed_model or "text-embedding-3-small",
            api_base=client_options.get("base_url"),
            default_headers=client_options.get("default_headers"),
        )

    def get_collection(self):
        return self.client.get_or_create_collection(
            name=self.collection_name,
            embedding_function=self.embed_fn,
        )

    def list_collections(self) -> list[str]:
        return [c.name for c in self.client.list_collections()]

    def _embed(self, texts: list[str], thread_id: str, prompt: str) -> list[list[float]]:
        self.budget_guard.check_budget_or_raise()
        model_name = self.cfg.embed_model or "text-embedding-3-small"
        response = self.embed_fn.client.embeddings.create(model=model_name, input=texts)
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None)
        self.budget_guard.log_usage(
            thread_id=thread_id,
            prompt=prompt,
            model_name=getattr(response, "model", None) or model_name,
            input_tokens=input_tokens,
            output_tokens=0,
        )
        return [entry.embedding for entry in sorted(response.data, key=lambda entry: entry.index)]

    def upsert_chunks(
        self,
        parent_doc_id: str,
        chunks: list[TextChunk],
        source_type: str,
        branch_code: str | None = "HQ",
        apply_pdpa_scrub: bool = True,
    ) -> int:
        if not chunks:
            return 0
        col = self.get_collection()

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict] = []
        for ch in chunks:
            clean_text = scrub_pdpa(ch.text) if apply_pdpa_scrub else ch.text
            tokens = estimate_tokens(clean_text)
            ids.append(ch.chunk_id)
            documents.append(clean_text)
            metadata = {
                "parent_doc_id": parent_doc_id,
                "source_type": source_type,
                "token_count": tokens,
                **ch.metadata,
            }
            if branch_code is not None:
                metadata["branch_code"] = branch_code.upper()
            else:
                metadata.pop("branch_code", None)
            metadatas.append(metadata)

        if self.budget_guard:
            embeddings = self._embed(
                documents, thread_id=f"embedding:{parent_doc_id}", prompt=f"Embed {source_type}"
            )
            col.delete(where={"parent_doc_id": parent_doc_id})
            col.upsert(ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings)
        else:
            col.delete(where={"parent_doc_id": parent_doc_id})
            col.upsert(ids=ids, documents=documents, metadatas=metadatas)
        return len(chunks)

    def ingest_document(
        self,
        parent_doc_id: str,
        source_type: str,
        branch_code: str | None = "HQ",
        text: str = "",
        strategy: str = "single",
        num_chunks: int = 1,
        chunk_size: int = 1000,
        chunk_overlap: int = 150,
        row_descriptions: list[str] | None = None,
        rows_per_chunk: int = 1,
        extra_metadata: dict | None = None,
        apply_pdpa_scrub: bool = True,
    ) -> int:
        chunks = build_chunks(
            doc_id=parent_doc_id,
            text=text,
            strategy=strategy,  # type: ignore[arg-type]
            num_chunks=num_chunks,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            row_descriptions=row_descriptions,
            rows_per_chunk=rows_per_chunk,
            base_metadata=extra_metadata,
        )
        return self.upsert_chunks(
            parent_doc_id=parent_doc_id,
            chunks=chunks,
            source_type=source_type,
            branch_code=branch_code,
            apply_pdpa_scrub=apply_pdpa_scrub,
        )

    def semantic_search(
        self,
        query_text: str,
        branch_code: str | None = None,
        source_type: str | None = None,
        n_results: int = 4,
    ) -> list[tuple[dict, str]]:
        col = self.get_collection()
        total = col.count()
        if total == 0:
            return []

        filters = []
        if branch_code and branch_code.upper() != "HQ":
            filters.append({"branch_code": branch_code.upper()})
        if source_type:
            filters.append({"source_type": source_type})

        where_clause = None
        if len(filters) == 1:
            where_clause = filters[0]
        elif len(filters) > 1:
            where_clause = {"$and": filters}

        if self.budget_guard:
            embedding = self._embed([query_text], thread_id="embedding:search", prompt="Embed search query")
            res = col.query(query_embeddings=embedding, n_results=min(n_results, total), where=where_clause)
        else:
            res = col.query(query_texts=[query_text], n_results=min(n_results, total), where=where_clause)
        metadatas = res["metadatas"]
        documents = res["documents"]
        if metadatas is None or documents is None:
            raise RuntimeError("ChromaDB query did not return metadata and documents")
        return list(zip(metadatas[0], documents[0], strict=True))

    def delete_document(self, parent_doc_id: str) -> None:
        self.get_collection().delete(where={"parent_doc_id": parent_doc_id})

    def delete_invoice_document(self, parent_doc_id: str) -> None:
        self.get_collection().delete(
            where={"$and": [{"parent_doc_id": parent_doc_id}, {"source_type": "invoice"}]}
        )