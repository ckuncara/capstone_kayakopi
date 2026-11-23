"""Persistent conversations for the OCR harness."""

import json
import time
from contextlib import closing

from langchain_core.messages import AIMessage, HumanMessage

from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository
from kaya_kopi.security.pdpa_scrubber import scrub_pdpa


def _redact(value):
    if isinstance(value, str):
        return scrub_pdpa(value)
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


class HarnessChatStore:
    def __init__(self, repo: SQLiteOpsRepository):
        self.repo = repo
        with closing(repo._connect()) as conn, conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS harness_chat_threads (
                    thread_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    history_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    tool_log_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS harness_chat_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                )"""
            )
            conn.execute(
                """INSERT OR IGNORE INTO harness_chat_metadata (key, value)
                   SELECT 'legacy_log_max_id', COALESCE(MAX(id), 0) FROM chat_logs"""
            )

    def list_threads(self) -> list[dict]:
        with closing(self.repo._connect()) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT thread_id, name FROM harness_chat_threads ORDER BY updated_at DESC"
                )
            ]

    def load(self, thread_id: str) -> dict | None:
        with closing(self.repo._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM harness_chat_threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        if row is None:
            return None
        history = json.loads(row["history_json"])
        context = json.loads(row["context_json"])
        return {
            "name": row["name"],
            "history": history,
            "messages": [
                HumanMessage(content=message["content"])
                if message["role"] == "user"
                else AIMessage(content=message["content"])
                for message in context
            ],
            "tool_log": json.loads(row["tool_log_json"]),
        }

    def save(self, thread_id: str, history: list, messages: list, tool_log: list) -> None:
        first_question = next(
            (item["content"] for item in history if item["role"] == "user"), "New Thread"
        )
        name = scrub_pdpa(first_question).strip().replace("\n", " ")[:60] or "New Thread"
        context = []
        for message in messages:
            if isinstance(message, HumanMessage):
                context.append({"role": "user", "content": str(message.content)})
            elif isinstance(message, AIMessage) and message.content and not message.tool_calls:
                context.append({"role": "assistant", "content": str(message.content)})
        with closing(self.repo._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO harness_chat_threads
                   (thread_id, name, history_json, context_json, tool_log_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(thread_id) DO UPDATE SET
                       history_json=excluded.history_json, context_json=excluded.context_json,
                       tool_log_json=excluded.tool_log_json, updated_at=excluded.updated_at""",
                (
                    thread_id,
                    name,
                    json.dumps(_redact(history), ensure_ascii=False),
                    json.dumps(_redact(context), ensure_ascii=False),
                    json.dumps(_redact(tool_log), ensure_ascii=False),
                    time.time_ns(),
                ),
            )

    def rename(self, thread_id: str, name: str) -> None:
        name = scrub_pdpa(name).strip()
        if not name:
            raise ValueError("Thread name cannot be empty.")
        with closing(self.repo._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE harness_chat_threads SET name = ? WHERE thread_id = ?",
                (name, thread_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Thread not found.")

    def legacy_log(self) -> list[dict]:
        with closing(self.repo._connect()) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT redacted_question, redacted_answer, created_at FROM chat_logs
                       WHERE id <= (SELECT CAST(value AS INTEGER) FROM harness_chat_metadata
                                    WHERE key = 'legacy_log_max_id')
                       ORDER BY id"""
                )
            ]

    def has_legacy_log(self) -> bool:
        with closing(self.repo._connect()) as conn:
            return conn.execute(
                """SELECT EXISTS(SELECT 1 FROM chat_logs
                   WHERE id <= (SELECT CAST(value AS INTEGER) FROM harness_chat_metadata
                                WHERE key = 'legacy_log_max_id'))"""
            ).fetchone()[0] == 1
