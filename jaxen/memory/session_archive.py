"""Session JSON 之外的可检索历史索引（SQLite + FTS5）。"""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class SessionArchive:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _open(self):
        """事务结束后显式关闭连接，避免 Windows 长时间占用 SQLite 文件。"""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._open() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    workspace_root TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    sequence_no INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    args_json TEXT NOT NULL DEFAULT '{}',
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, sequence_no)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    content,
                    session_id UNINDEXED,
                    message_id UNINDEXED,
                    tokenize='unicode61'
                );
                """
            )

    def sync_session(self, session):
        session_id = str(session["id"])
        created_at = str(session.get("created_at", ""))
        workspace_root = str(session.get("workspace_root", ""))
        with self._open() as connection:
            connection.execute(
                """INSERT INTO sessions(session_id, workspace_root, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       workspace_root=excluded.workspace_root,
                       updated_at=excluded.updated_at""",
                (session_id, workspace_root, created_at, created_at),
            )
            for sequence_no, item in enumerate(session.get("history", [])):
                archive_sequence = int(item.get("archive_sequence", sequence_no))
                self._insert_message(connection, session_id, archive_sequence, item)

    def append(self, session, sequence_no, item):
        session_id = str(session["id"])
        created_at = str(item.get("created_at", session.get("created_at", "")))
        with self._open() as connection:
            connection.execute(
                """INSERT INTO sessions(session_id, workspace_root, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at""",
                (session_id, str(session.get("workspace_root", "")), str(session.get("created_at", "")), created_at),
            )
            return self._insert_message(connection, session_id, int(sequence_no), item)

    @staticmethod
    def _insert_message(connection, session_id, sequence_no, item):
        args_json = json.dumps(item.get("args", {}), ensure_ascii=False, sort_keys=True)
        content = str(item.get("content", ""))
        search_text = "\n".join(
            part for part in (str(item.get("name", "")), args_json, content) if part
        )
        cursor = connection.execute(
            """INSERT OR IGNORE INTO messages(
                   session_id, sequence_no, role, name, args_json, content, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                sequence_no,
                str(item.get("role", "")),
                str(item.get("name", "")),
                args_json,
                content,
                str(item.get("created_at", "")),
            ),
        )
        if cursor.rowcount == 0:
            row = connection.execute(
                "SELECT id FROM messages WHERE session_id=? AND sequence_no=?",
                (session_id, sequence_no),
            ).fetchone()
            message_id = int(row["id"])
        else:
            message_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT OR REPLACE INTO messages_fts(rowid, content, session_id, message_id) VALUES (?, ?, ?, ?)",
            (message_id, search_text, session_id, str(message_id)),
        )
        return message_id

    def search(self, query, limit=5, include_tools=False, session_id=""):
        query = str(query).strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(int(limit), 20))
        role_clause = "" if include_tools else " AND m.role IN ('user', 'assistant')"
        session_clause = " AND m.session_id=?" if session_id else ""
        terms = re.findall(r"[A-Za-z0-9_\u3400-\u9fff]+", query)
        fts_query = " AND ".join(f'"{term}"' for term in terms) or f'"{query.replace(chr(34), chr(34) * 2)}"'
        parameters = [fts_query]
        if session_id:
            parameters.append(str(session_id))
        parameters.append(limit)
        sql = f"""
            SELECT m.id, m.session_id, m.sequence_no, m.role, m.name, m.content, m.created_at,
                   bm25(messages_fts) AS rank
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            WHERE messages_fts MATCH ?{role_clause}{session_clause}
            ORDER BY rank, m.id DESC
            LIMIT ?
        """
        with self._open() as connection:
            try:
                rows = connection.execute(sql, parameters).fetchall()
            except sqlite3.OperationalError:
                rows = []
            # unicode61 对无空格中文的切词并不总符合查询习惯；LIKE 是确定性兜底。
            if not rows:
                parameters = [f"%{query}%"]
                if session_id:
                    parameters.append(str(session_id))
                parameters.append(limit)
                rows = connection.execute(
                    f"""SELECT m.id, m.session_id, m.sequence_no, m.role, m.name, m.content,
                               m.created_at, 0 AS rank
                        FROM messages m
                        WHERE m.content LIKE ?{role_clause}{session_clause}
                        ORDER BY m.id DESC LIMIT ?""",
                    parameters,
                ).fetchall()
        return [dict(row) for row in rows]

    def read(self, session_id, start=0, limit=20, around_message_id=0, include_tools=True):
        session_id = str(session_id).strip()
        if not session_id:
            raise ValueError("session_id must not be empty")
        limit = max(1, min(int(limit), 50))
        start = max(0, int(start))
        role_clause = "" if include_tools else " AND role IN ('user', 'assistant')"
        with self._open() as connection:
            if int(around_message_id or 0):
                anchor = connection.execute(
                    "SELECT sequence_no FROM messages WHERE id=? AND session_id=?",
                    (int(around_message_id), session_id),
                ).fetchone()
                if anchor is None:
                    raise ValueError("around_message_id does not belong to this session")
                start = max(0, int(anchor["sequence_no"]) - limit // 2)
            rows = connection.execute(
                f"""SELECT id, session_id, sequence_no, role, name, content, created_at, 0 AS rank
                    FROM messages
                    WHERE session_id=? AND sequence_no>=?{role_clause}
                    ORDER BY sequence_no LIMIT ?""",
                (session_id, start, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def render_results(results):
        if not results:
            return "(no archived session matches)"
        lines = []
        for result in results:
            content = " ".join(str(result["content"]).split())
            if len(content) > 500:
                content = content[:497] + "..."
            source = (
                f"session={result['session_id']} message={result['id']} "
                f"sequence={result['sequence_no']} role={result['role']}"
            )
            if result.get("name"):
                source += f" tool={result['name']}"
            lines.append(f"[source {source}]\n{content}")
        return "\n\n".join(lines)
