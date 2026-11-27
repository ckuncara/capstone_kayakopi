import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from kaya_kopi.paths import project_root

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(project_root() / ".env")


def _sqlite_db_path() -> Path:
    load_dotenv(project_root() / ".env")
    return Path(os.getenv("LOCAL_SQLITE_PATH", str(REPO_ROOT / "data" / "kaya_ops.db")))


CHAT_MODEL_KEYS = ("KAYA_OPENAI_MODEL", "OPENAI_CHAT_MODEL")
MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
LATEST_CHAT_MODELS = (
    "gpt-6.2-astra",
    "gpt-6.2-sol",
    "gpt-6.1-terra",
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
)
SUGGESTED_CHAT_MODELS = LATEST_CHAT_MODELS + (
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4.1-mini",
    "gpt-4.1",
    "openai/gpt-4.1",
)


def suggested_chat_models() -> tuple[str, ...]:
    """Return editor suggestions; any syntactically valid model name is accepted."""
    models = set(SUGGESTED_CHAT_MODELS)
    extra = os.getenv("KAYA_CHAT_MODELS", "")
    models.update(name.strip() for name in extra.split(",") if name.strip())
    return tuple(sorted(models))


def _write_env_values(path: Path, values: dict[str, str]) -> None:
    """Update or append simple KEY=value entries without disturbing other lines."""
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = original.splitlines()
    for key, value in values.items():
        pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=.*$")
        for index, line in enumerate(lines):
            if pattern.match(line):
                lines[index] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    ending = "\n" if not original or original.endswith(("\n", "\r")) else ""
    path.write_text("\n".join(lines) + ending, encoding="utf-8")


def set_chat_model(model: str) -> str:
    """Persist the chat model choice for both apps and the current process."""
    cleaned = model.strip()
    if not MODEL_NAME_PATTERN.fullmatch(cleaned):
        raise ValueError("Model name must be 1-128 characters using letters, numbers, dot, underscore, colon, slash or hyphen.")
    # Model compatibility is provider-specific, so accept any safe name and let API
    # calls surface an actionable error if the provider rejects it.
    for key in CHAT_MODEL_KEYS:
        os.environ[key] = cleaned
    values = {key: cleaned for key in CHAT_MODEL_KEYS}
    root = project_root()
    for filename in (".env", ".env.example"):
        _write_env_values(root / filename, values)
    return cleaned


@dataclass(frozen=True)
class AppConfig:
    api_key: str = os.getenv("OPENAI_API_KEY", "")
    llm_proxy_url: str = os.getenv("LLM_PROXY_URL", "").rstrip("/")
    supabase_url: str = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "")
    # Configurable OpenAI models with defaults if not found
    ocr_model: str = os.getenv("OPENAI_OCR_MODEL", "gpt-6-luna")
    chat_model: str = field(default_factory=lambda: os.getenv("OPENAI_CHAT_MODEL", "gpt-6-luna"))
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
    sqlite_db_path: Path = field(default_factory=_sqlite_db_path)
    chroma_dir: Path = Path(
        os.getenv("LOCAL_CHROMA_DIR", str(REPO_ROOT / "data" / "local_chroma_db"))
    )
    chroma_host: str = os.getenv("CHROMA_HOST", "")
    chroma_port: int = int(os.getenv("CHROMA_PORT", "8000"))
    chroma_collection: str = os.getenv("CHROMA_COLLECTION", "kaya_kopi_knowledge")
