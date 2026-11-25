"""Bridge to the Ops Copilot chat store, kept entirely inside Upload Desk.

The copilot app discovers chats from LangGraph checkpoints in ``KAYA_CHAT_DB``
(or ``./.kaya_chat.sqlite3``) and reads names from ``conversation_names``. This
bridge writes the same structures so the unmodified copilot lists and opens
Upload Desk chats, while this app can list and open copilot chats.

Database lookup is deliberately tolerant: a container may expose an unwritable
configured path, and the copilot may have been started from a different working
directory. The bridge first uses an existing chat database, then falls back to
a writable location instead of failing with ``unable to open database file``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from kaya_kopi.paths import project_root
from kaya_kopi.security.guardrails import protect_answer, protect_text


DB_NAME = ".kaya_chat.sqlite3"


def _dedupe(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        try:
            key = path.resolve()
        except OSError:
            key = path.absolute()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _candidate_paths() -> list[Path]:
    """Paths where either app may keep its chat database."""
    configured = os.getenv("KAYA_CHAT_DB", "").strip()
    root = project_root()
    candidates: list[Path] = []
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        candidates.append(path)
    candidates.extend([
        Path.cwd() / DB_NAME,
        root / DB_NAME,
        # Streamlit entry point in the copilot directory uses its own cwd.
        root / "src" / "kaya_kopi" / "copilot" / DB_NAME,
        root / "data" / DB_NAME,
        Path.home() / DB_NAME,
        Path(tempfile.gettempdir()) / "kaya_kopi" / DB_NAME,
    ])
    return _dedupe(candidates)


def _try_connect(path: Path, *, warn: bool = True) -> sqlite3.Connection | None:
    connection: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection
    except (OSError, sqlite3.Error) as error:
        if connection is not None:
            connection.close()
        if warn:
            logging.warning("Chat database at %s is unavailable: %s", path, error)
        return None


def _has_chat_tables(connection: sqlite3.Connection) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'"
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def chat_db_path() -> Path:
    """Return the copilot chat DB, or a writable fallback.

    An explicit ``KAYA_CHAT_DB`` is authoritative when it can be opened. When
    it is unset or unavailable, search the locations used by the copilot entry
    point and pick the database with the newest checkpoint. If none exists,
    use the first writable location instead of raising an SQLite error.
    """
    candidates = _candidate_paths()
    if os.getenv("KAYA_CHAT_DB", "").strip() and candidates:
        connection = _try_connect(candidates[0], warn=False)
        if connection is not None:
            connection.close()
            return candidates[0]

    best_path: Path | None = None
    best_write = -1
    for path in candidates:
        if not path.is_file():
            continue
        connection = _try_connect(path, warn=False)
        if connection is None:
            continue
        try:
            if not _has_chat_tables(connection):
                continue
            row = connection.execute("SELECT MAX(rowid) FROM checkpoints").fetchone()
            last_write = int(row[0]) if row and row[0] is not None else 0
            if last_write > best_write:
                best_path, best_write = path, last_write
        except sqlite3.Error:
            continue
        finally:
            connection.close()
    if best_path is not None:
        return best_path

    for path in candidates:
        connection = _try_connect(path, warn=False)
        if connection is not None:
            connection.close()
            return path
    raise sqlite3.OperationalError("No writable SQLite chat database is available")


def _all_chat_paths() -> list[Path]:
    """Use one chat database as the single shared source of truth."""
    return [chat_db_path()]


def _connect() -> sqlite3.Connection:
    path = chat_db_path()
    connection = _try_connect(path)
    if connection is None:
        raise sqlite3.OperationalError(f"unable to open database file: {path}")
    return connection


def _saver(connection: sqlite3.Connection) -> SqliteSaver:
    return SqliteSaver(connection)


def _thread_rows(path: Path) -> list[dict]:
    connection = _try_connect(path, warn=False)
    if connection is None:
        return []
    try:
        if not _has_chat_tables(connection):
            return []
        rows = connection.execute(
            "SELECT thread_id, MAX(rowid) AS last_write FROM checkpoints "
            "GROUP BY thread_id"
        ).fetchall()
        names: dict[str, str] = {}
        try:
            names = {
                row[0]: row[1]
                for row in connection.execute("SELECT thread_id, name FROM conversation_names")
            }
        except sqlite3.Error:
            pass
        return [
            {
                "thread_id": row[0],
                "name": names.get(row[0], ""),
                "last_write": row[1] or 0,
            }
            for row in rows
        ]
    except sqlite3.Error as error:
        logging.warning("Could not list chats in %s: %s", path, error)
        return []
    finally:
        connection.close()


def list_threads() -> list[dict]:
    """Return chat ids and names from both apps, newest checkpoint first."""
    merged: dict[str, dict] = {}
    for path in _all_chat_paths():
        for row in _thread_rows(path):
            thread_id = row["thread_id"]
            current = merged.get(thread_id)
            if current is None or row["last_write"] > current["last_write"]:
                merged[thread_id] = row
    ordered = sorted(
        merged.values(), key=lambda row: (-row["last_write"], row["thread_id"])
    )
    return [
        {
            "thread_id": row["thread_id"],
            "name": row["name"] or f"Conversation {index}",
        }
        for index, row in enumerate(ordered, start=1)
    ]


def _display_messages(messages: list) -> list[dict]:
    history: list[dict] = []
    turn_id = None
    for message in messages:
        if isinstance(message, HumanMessage):
            turn_id = message.id
            history.append({
                "role": "user",
                "content": protect_text(str(message.content)),
                "turn_id": turn_id,
            })
        elif isinstance(message, AIMessage) and message.content:
            history.append({
                "role": "assistant",
                "content": protect_answer(str(message.content)),
                "turn_id": turn_id,
            })
    return history


def load_messages(thread_id: str) -> list[dict]:
    """Read one copilot checkpoint as plain chat messages for display."""
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    for path in _all_chat_paths():
        connection = _try_connect(path, warn=False)
        if connection is None:
            continue
        try:
            if not _has_chat_tables(connection):
                continue
            checkpoint = _saver(connection).get_tuple(config)
            if checkpoint is None:
                continue
            messages = checkpoint.checkpoint.get("channel_values", {}).get("messages", [])
            if messages:
                return _display_messages(messages)
        except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
            logging.warning("Could not read copilot chat %s from %s: %s", thread_id, path, error)
        finally:
            connection.close()
    return []


def has_thread(thread_id: str) -> bool:
    for path in _all_chat_paths():
        connection = _try_connect(path, warn=False)
        if connection is None:
            continue
        try:
            if not _has_chat_tables(connection):
                continue
            if connection.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id=? LIMIT 1", (thread_id,)
            ).fetchone():
                return True
        except sqlite3.Error:
            continue
        finally:
            connection.close()
    return False


def save_thread(thread_id: str, name: str, messages: list[dict]) -> None:
    """Mirror display messages into a checkpoint the copilot can continue."""
    connection = _connect()
    try:
        graph_messages = []
        for message in messages:
            if message.get("role") == "user":
                graph_messages.append(HumanMessage(
                    content=str(message.get("content", "")),
                    id=str(message.get("turn_id") or uuid.uuid4()),
                ))
            elif message.get("role") == "assistant" and message.get("content"):
                graph_messages.append(AIMessage(content=str(message["content"])))
        if not graph_messages:
            return
        version = time.time_ns()
        checkpoint = {
            "v": 1,
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "channel_values": {"messages": graph_messages},
            "channel_versions": {"messages": version},
            "versions_seen": {},
        }
        _saver(connection).put(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            checkpoint,
            {"source": "upload-desk", "step": 1, "writes": {}},
            {"messages": version},
        )
        if name:
            rename_thread(thread_id, name)
    finally:
        connection.close()


def ensure_thread(thread_id: str, name: str, messages: list[dict]) -> None:
    """Backfill a chat only when neither app has seen it."""
    if not has_thread(thread_id):
        save_thread(thread_id, name, messages)


def rename_thread(thread_id: str, name: str) -> None:
    name = str(name or "").strip()[:80]
    if not name:
        return
    for path in _all_chat_paths():
        connection = _try_connect(path, warn=False)
        if connection is None:
            continue
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS conversation_names "
                "(thread_id TEXT PRIMARY KEY, name TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO conversation_names (thread_id,name) VALUES (?,?) "
                "ON CONFLICT(thread_id) DO UPDATE SET name=excluded.name",
                (thread_id, name),
            )
            connection.commit()
        except sqlite3.Error as error:
            logging.warning("Could not rename chat %s in %s: %s", thread_id, path, error)
        finally:
            connection.close()


def delete_thread(thread_id: str) -> None:
    for path in _all_chat_paths():
        connection = _try_connect(path, warn=False)
        if connection is None:
            continue
        try:
            if _has_chat_tables(connection):
                _saver(connection).delete_thread(thread_id)
            try:
                connection.execute(
                    "DELETE FROM conversation_names WHERE thread_id=?", (thread_id,)
                )
                connection.commit()
            except sqlite3.Error:
                pass
        finally:
            connection.close()
