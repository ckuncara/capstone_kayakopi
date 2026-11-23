import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AppConfig:
    api_key: str = os.getenv("OPENAI_API_KEY", "")
    # Configurable OpenAI models with defaults if not found
    ocr_model: str = os.getenv("OPENAI_OCR_MODEL", "gpt-4o-mini")
    chat_model: str = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
    embed_model: str = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")

    # Monthly Budget Guardrail (USD)
    monthly_budget_usd: float = float(os.getenv("MONTHLY_BUDGET_USD", "50.00"))

    # Storage paths matching Mermaid architecture
    supplier_invoice_dir: Path = Path(
        os.getenv("SUPPLIER_INVOICE_DIR", str(REPO_ROOT / "data" / "images" / "supplier_invoices"))
    )
    audited_csv_path: Path = Path(
        os.getenv(
            "AUDITED_INVOICE_CSV",
            str(REPO_ROOT / "data" / "images" / "supplier_invoices" / "supplier_invoices_audited.csv"),
        )
    )
    sqlite_db_path: Path = Path(
        os.getenv("LOCAL_SQLITE_PATH", str(REPO_ROOT / "data" / "kaya_ops.db"))
    )
    chroma_dir: Path = Path(
        os.getenv("LOCAL_CHROMA_DIR", str(REPO_ROOT / "data" / "local_chroma_db"))
    )
    chroma_host: str = os.getenv("CHROMA_HOST", "")
    chroma_port: int = int(os.getenv("CHROMA_PORT", "8000"))
    chroma_collection: str = os.getenv("CHROMA_COLLECTION", "kaya_kopi_knowledge")
