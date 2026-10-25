from __future__ import annotations

from dataclasses import dataclass
import re
from contextlib import contextmanager
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .consolidator import ConsolidationResult

@dataclass(frozen=True)
class Relationship:
    id: int
    subject: str
    relation: str
    object: str
    confidence: float
    created_at: str
    updated_at: str

@dataclass(frozen=True)
class Memory:
    id: int
    category: str
    key: str
    value: str
    statement: str
    confidence: float
    created_at: str
    updated_at: str
    kind: str = "fact"
    scope: str = "user"
    subject: str = "user"
    importance: int = 5
    status: str = "active"
    source_session_id: int | None = None

@dataclass(frozen=True)
class MemoryHistory:
    id: int
    category: str
    key: str
    value: str
    statement: str
    confidence: float
    recorded_at: str

@dataclass(frozen=True)
class Contact:
    id: int
    name: str
    email: str
    created_at: str
    updated_at: str    


class LongTermMemory:
    """Persistent memory backed by SQLite."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._initialize()

    @contextmanager
    def _connect(self):
        """Yield a configured SQLite connection and always close it."""
        connection = sqlite3.connect(
            self.database_path,
            timeout=15.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            # Preserve sqlite's transaction semantics while guaranteeing that
            # every short-lived connection is closed at the end of the block.
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.8,
                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(category, key)
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.8,
                    recorded_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(memories)"
                ).fetchall()
            }

            if "statement" not in columns:
                connection.execute(
                    """
                    ALTER TABLE memories
                    ADD COLUMN statement TEXT
                    """
                )

                connection.execute(
                    """
                    UPDATE memories
                    SET statement =
                        CASE
                            WHEN category = 'user'
                                THEN 'The user has '
                                     || key
                                     || ': '
                                     || value
                            ELSE
                                category
                                || ': '
                                || key
                                || ': '
                                || value
                        END
                    WHERE statement IS NULL
                    """
                )

            if "confidence" not in columns:
                connection.execute(
                    """
                    ALTER TABLE memories
                    ADD COLUMN confidence REAL
                        NOT NULL DEFAULT 0.8
                    """
                )

            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(
                    category,
                    key,
                    value,
                    statement,
                    content='memories',
                    content_rowid='id'
                )
                """
            )

            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memories_ai
                AFTER INSERT ON memories
                BEGIN
                    INSERT INTO memories_fts(
                        rowid,
                        category,
                        key,
                        value,
                        statement
                    )
                    VALUES (
                        new.id,
                        new.category,
                        new.key,
                        new.value,
                        new.statement
                    );
                END
                """
            )

            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memories_ad
                AFTER DELETE ON memories
                BEGIN
                    INSERT INTO memories_fts(
                        memories_fts,
                        rowid,
                        category,
                        key,
                        value,
                        statement
                    )
                    VALUES (
                        'delete',
                        old.id,
                        old.category,
                        old.key,
                        old.value,
                        old.statement
                    );
                END
                """
            )

            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memories_au
                AFTER UPDATE ON memories
                BEGIN
                    INSERT INTO memories_fts(
                        memories_fts,
                        rowid,
                        category,
                        key,
                        value,
                        statement
                    )
                    VALUES (
                        'delete',
                        old.id,
                        old.category,
                        old.key,
                        old.value,
                        old.statement
                    );

                    INSERT INTO memories_fts(
                        rowid,
                        category,
                        key,
                        value,
                        statement
                    )
                    VALUES (
                        new.id,
                        new.category,
                        new.key,
                        new.value,
                        new.statement
                    );
                END
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    ended_at TEXT,
                    summary TEXT
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS relationships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    object TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.8,
                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(subject, relation, object)
                )
                """
            )




            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL,
                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            # Chats ADVI has messaged (WhatsApp, Telegram) and how a name was misheard,
            # so "CU" can be recognised as "Siyu" next time.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_names (
                    app TEXT NOT NULL,
                    name TEXT NOT NULL,
                    heard_as TEXT NOT NULL DEFAULT '',
                    last_used TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (app, name, heard_as)
                )
                """
            )

            columns = {row["name"] for row in connection.execute("PRAGMA table_info(memories)").fetchall()}
            rich_columns = {
                "kind": "TEXT NOT NULL DEFAULT 'fact'",
                "scope": "TEXT NOT NULL DEFAULT 'user'",
                "subject": "TEXT NOT NULL DEFAULT 'user'",
                "importance": "INTEGER NOT NULL DEFAULT 5",
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "source_session_id": "INTEGER",
            }
            for name, definition in rich_columns.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")

            connection.execute("""
                CREATE TABLE IF NOT EXISTS memory_episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 5,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def all(self) -> list[Memory]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    category,
                    key,
                    value,
                    statement,
                    confidence,
                    created_at,
                    updated_at,
                    kind,
                    scope,
                    subject,
                    importance,
                    status,
                    source_session_id
                FROM memories
                ORDER BY updated_at DESC
                """
            ).fetchall()

        return [
            Memory(**dict(row))
            for row in rows
        ]

    def recent(self, limit: int = 32) -> list[Memory]:
        """Return a small recent window for bounded retrieval candidate generation."""
        limit = max(0, min(int(limit), 256))
        if limit == 0:
            return []

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    category,
                    key,
                    value,
                    statement,
                    confidence,
                    created_at,
                    updated_at,
                    kind,
                    scope,
                    subject,
                    importance,
                    status,
                    source_session_id
                FROM memories
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [Memory(**dict(row)) for row in rows]

    def remember(
        self,
        category: str,
        key: str,
        value: str,
        statement: str | None = None,
        confidence: float = 0.8,
    ) -> None:
        category = category.strip()
        key = key.strip()
        value = value.strip()

        if statement is None:
            statement = f"The user has {key}: {value}."

        statement = statement.strip()

        if (
            not category
            or not key
            or not value
            or not statement
        ):
            return

        confidence = max(
            0.0,
            min(1.0, float(confidence)),
        )

        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT
                    category,
                    key,
                    value,
                    statement,
                    confidence
                FROM memories
                WHERE category = ?
                  AND key = ?
                """,
                (category, key),
            ).fetchone()

            # First occurrence.
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO memories (
                        category,
                        key,
                        value,
                        statement,
                        confidence
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        category,
                        key,
                        value,
                        statement,
                        confidence,
                    ),
                )
                return

            # Same fact.
            # Keep the existing memory unless the new evidence
            # has higher confidence.
            if (
                existing["value"] == value
                and existing["statement"] == statement
            ):
                if confidence > existing["confidence"]:
                    connection.execute(
                        """
                        UPDATE memories
                        SET confidence = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE category = ?
                          AND key = ?
                        """,
                        (
                            confidence,
                            category,
                            key,
                        ),
                    )

                return

            # Conflicting weaker evidence must not overwrite
            # the stronger existing memory.
            if confidence < existing["confidence"]:
                return

            # The new conflicting evidence is equally or more
            # confident. Preserve the previous value in history.
            connection.execute(
                """
                INSERT INTO memory_history (
                    category,
                    key,
                    value,
                    statement,
                    confidence
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    existing["category"],
                    existing["key"],
                    existing["value"],
                    existing["statement"],
                    existing["confidence"],
                ),
            )

            connection.execute(
                """
                UPDATE memories
                SET value = ?,
                    statement = ?,
                    confidence = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE category = ?
                  AND key = ?
                """,
                (
                    value,
                    statement,
                    confidence,
                    category,
                    key,
                ),
            )

    def remember_rich(
        self, *, kind: str, scope: str, subject: str, key: str, value: str,
        statement: str, confidence: float = 0.8, importance: int = 5,
        source_session_id: int | None = None,
    ) -> None:
        kind = (kind or "fact").strip().lower()
        scope = (scope or "user").strip().lower()
        subject = (subject or "user").strip()
        self.remember(category=kind, key=key, value=value, statement=statement, confidence=confidence)
        with self._connect() as connection:
            connection.execute(
                """UPDATE memories SET kind=?, scope=?, subject=?, importance=?, status='active', source_session_id=?, updated_at=CURRENT_TIMESTAMP
                   WHERE category=? AND key=?""",
                (kind, scope, subject, max(1, min(10, int(importance))), source_session_id, kind, key),
            )

    def save_episode(self, title: str, summary: str, *, session_id: int | None = None, importance: int = 5) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO memory_episodes(session_id,title,summary,importance) VALUES (?,?,?,?)",
                (session_id, title.strip()[:200], summary.strip()[:4000], max(1, min(10, int(importance)))),
            )
            return int(cursor.lastrowid)

    def history(
        self,
        category: str,
        key: str,
        limit: int = 20,
    ) -> list[MemoryHistory]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    category,
                    key,
                    value,
                    statement,
                    confidence,
                    recorded_at
                FROM memory_history
                WHERE category = ?
                AND key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (category, key, limit),
            ).fetchall()

        return [
            MemoryHistory(**dict(row))
            for row in rows
        ]  

    @staticmethod
    def normalize_email(
        value: str,
    ) -> str | None:
        """
        Return a plain valid email address or None.

        This is intentionally strict because contact data is
        security/reliability-sensitive and is passed directly to
        Gmail.
        """
        text = value.strip()

        if not text:
            return None

        # Accept exactly one plain email address.
        match = re.fullmatch(
            r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+",
            text,
        )

        if match is None:
            return None

        return match.group(0).lower()

    @classmethod
    def extract_email(
        cls,
        value: str,
    ) -> str | None:
        """
        Extract exactly one email address from free-form user text.

        Returns None when there is no email or when multiple
        addresses are present.
        """
        text = value.strip()

        if not text:
            return None

        matches = re.findall(
            r"(?<![\w.+-])"
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
            r"@"
            r"[A-Za-z0-9-]+"
            r"(?:\.[A-Za-z0-9-]+)+"
            r"(?![\w.-])",
            text,
        )

        # A contact reply should contain exactly one address.
        unique_matches = list(
            dict.fromkeys(
                match.lower()
                for match in matches
            )
        )

        if len(unique_matches) != 1:
            return None

        return cls.normalize_email(
            unique_matches[0]
        )    


    def get_contact(
        self,
        name: str,
    ) -> Contact | None:
        name = name.strip()

        if not name:
            return None

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    name,
                    email,
                    created_at,
                    updated_at
                FROM contacts
                WHERE LOWER(name) = LOWER(?)
                """,
                (name,),
            ).fetchone()

        if row is None:
            return None

        email = self.normalize_email(
            row["email"]
        )

        if email is None:
            # Existing database row is corrupt.
            # Never return it to Gmail.
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM contacts WHERE id = ?",
                    (row["id"],),
                )

            return None

        return Contact(
            id=row["id"],
            name=row["name"],
            email=email,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )  



    def find_contacts(
        self,
        query: str,
        limit: int = 10,
    ) -> list[Contact]:
        """Find contacts by exact, prefix, or contained name match.

        Exact case-insensitive matches are returned first. Results are bounded
        so a natural-language recipient lookup cannot trigger an unbounded
        contact scan. Invalid stored addresses are ignored and cleaned up.
        """
        query = query.strip()
        if not query or limit < 1:
            return []

        pattern = f"%{query}%"

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, email, created_at, updated_at
                FROM contacts
                WHERE LOWER(name) = LOWER(?)
                   OR name LIKE ? COLLATE NOCASE
                ORDER BY
                    CASE WHEN LOWER(name) = LOWER(?) THEN 0 ELSE 1 END,
                    LENGTH(name),
                    name COLLATE NOCASE
                LIMIT ?
                """,
                (query, pattern, query, int(limit)),
            ).fetchall()

        contacts: list[Contact] = []
        invalid_ids: list[int] = []
        for row in rows:
            email = self.normalize_email(row["email"])
            if email is None:
                invalid_ids.append(int(row["id"]))
                continue
            contacts.append(
                Contact(
                    id=row["id"],
                    name=row["name"],
                    email=email,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )

        if invalid_ids:
            with self._connect() as connection:
                connection.executemany(
                    "DELETE FROM contacts WHERE id = ?",
                    [(contact_id,) for contact_id in invalid_ids],
                )

        return contacts

    def list_contacts(self, limit: int = 100) -> list[Contact]:
        """Return a bounded list of valid stored contacts."""
        if limit < 1:
            return []

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, email, created_at, updated_at
                FROM contacts
                ORDER BY name COLLATE NOCASE
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()

        contacts: list[Contact] = []
        invalid_ids: list[int] = []
        for row in rows:
            email = self.normalize_email(row["email"])
            if email is None:
                invalid_ids.append(int(row["id"]))
                continue
            contacts.append(
                Contact(
                    id=row["id"],
                    name=row["name"],
                    email=email,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )

        if invalid_ids:
            with self._connect() as connection:
                connection.executemany(
                    "DELETE FROM contacts WHERE id = ?",
                    [(contact_id,) for contact_id in invalid_ids],
                )

        return contacts

    def remember_chat(self, app: str, name: str, heard_as: str = "") -> None:
        """Record a chat ADVI messaged; ``heard_as`` is how voice misheard it, if it did."""
        app, name = app.strip().casefold(), name.strip()
        heard_as = heard_as.strip().casefold() if heard_as.strip().casefold() != name.casefold() else ""
        if not app or not name:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_names (app, name, heard_as) VALUES (?, ?, ?)
                ON CONFLICT(app, name, heard_as) DO UPDATE SET last_used = CURRENT_TIMESTAMP
                """,
                (app, name, heard_as),
            )

    def known_chats(self, app: str, limit: int = 200) -> list[tuple[str, str]]:
        """(name, heard_as) pairs for an app, most recently used first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, heard_as FROM chat_names WHERE app = ? ORDER BY last_used DESC LIMIT ?",
                (app.strip().casefold(), int(limit)),
            ).fetchall()
        return [(row["name"], row["heard_as"]) for row in rows]


    def save_contact(
        self,
        name: str,
        email: str,
    ) -> bool:
        name = name.strip()

        if not name:
            return False

        normalized_email = self.normalize_email(
            email
        )

        if normalized_email is None:
            return False

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO contacts (
                    name,
                    email
                )
                VALUES (?, ?)
                ON CONFLICT(name)
                DO UPDATE SET
                    email = excluded.email,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    name,
                    normalized_email,
                ),
            )
        return True



    def recent_sessions(
        self,
        limit: int = 3,
    ) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    started_at,
                    ended_at,
                    summary
                FROM sessions
                WHERE summary IS NOT NULL
                AND summary != ''
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [dict(row) for row in rows]        

    def recall(
        self,
        category: str,
        key: str,
    ) -> Memory | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    category,
                    key,
                    value,
                    statement,
                    confidence,
                    created_at,
                    updated_at,
                    kind,
                    scope,
                    subject,
                    importance,
                    status,
                    source_session_id
                FROM memories
                WHERE category = ?
                  AND key = ?
                """,
                (category, key),
            ).fetchone()

        if row is None:
            return None

        return Memory(**dict(row))

    def search(
        self,
        query: str,
        limit: int = 15,
    ) -> list[Memory]:
        query = query.strip()

        if not query:
            return []

        import re

        # Normalize punctuation so queries such as:
        # "father's", "father?", "father's name?"
        # are searched as normal terms.
        normalized = re.sub(
            r"[^\w\s]",
            " ",
            query.lower(),
        )

        stopwords = {
            "what",
            "is",
            "my",
            "the",
            "a",
            "an",
            "of",
            "to",
            "do",
            "does",
            "did",
            "me",
            "i",
            "am",
            "are",
            "was",
            "were",
            "who",
            "where",
            "when",
            "how",
            "can",
            "you",
            "tell",
            "about",
        }

        terms = [
            term
            for term in normalized.split()
            if len(term) >= 2
            and term not in stopwords
        ]

        if not terms:
            return []

        fts_query = " OR ".join(terms)

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    rowid
                FROM memories_fts
                WHERE memories_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()

            if not rows:
                return []

            ids = [row["rowid"] for row in rows]

            placeholders = ",".join(
                "?" for _ in ids
            )

            memory_rows = connection.execute(
                f"""
                SELECT
                    id,
                    category,
                    key,
                    value,
                    statement,
                    confidence,
                    created_at,
                    updated_at,
                    kind,
                    scope,
                    subject,
                    importance,
                    status,
                    source_session_id
                FROM memories
                WHERE id IN ({placeholders})
                """,
                ids,
            ).fetchall()

        by_id = {
            row["id"]: Memory(**dict(row))
            for row in memory_rows
        }

        return [
            by_id[row_id]
            for row_id in ids
            if row_id in by_id
        ]

    def forget(
            self,
            category: str,
            key: str,
        ) -> None:
            category = category.strip()
            key = key.strip()

            if not category or not key:
                return

            with self._connect() as connection:
                connection.execute(
                    """
                    DELETE FROM memories
                    WHERE category = ?
                    AND key = ?
                    """,
                    (category, key),
                )

                connection.execute(
                    """
                    DELETE FROM memory_history
                    WHERE category = ?
                    AND key = ?
                    """,
                    (category, key),
                )

    def clear(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM memories"
            )

    def start_session(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO sessions DEFAULT VALUES"
            )
            return int(cursor.lastrowid)

    def end_session(
        self,
        session_id: int,
        summary: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET ended_at = CURRENT_TIMESTAMP,
                    summary = ?
                WHERE id = ?
                """,
                (summary, session_id),
            )
    
        
    def remember_relationship(
                self,
                subject: str,
                relation: str,
                object: str,
                confidence: float = 0.8,
            ) -> None:
                subject = subject.strip()
                relation = relation.strip()
                object = object.strip()

                if not subject or not relation or not object:
                    return

                confidence = max(
                    0.0,
                    min(1.0, float(confidence)),
                )

                with self._connect() as connection:
                    existing = connection.execute(
                        """
                        SELECT
                            id,
                            confidence
                        FROM relationships
                        WHERE subject = ?
                        AND relation = ?
                        AND object = ?
                        """,
                        (
                            subject,
                            relation,
                            object,
                        ),
                    ).fetchone()

                    if existing is None:
                        connection.execute(
                            """
                            INSERT INTO relationships (
                                subject,
                                relation,
                                object,
                                confidence
                            )
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                subject,
                                relation,
                                object,
                                confidence,
                            ),
                        )
                        return

                    if confidence > existing["confidence"]:
                        connection.execute(
                            """
                            UPDATE relationships
                            SET confidence = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (
                                confidence,
                                existing["id"],
                            ),
                        )
            
                # Same relationship already exists.
                # Keep the strongest confidence.
                if confidence > existing["confidence"]:
                    connection.execute(
                        """
                        UPDATE relationships
                        SET confidence = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            confidence,
                            existing["id"],
                        ),
                    )               

    def find_relationships(
        self,
        subject: str | None = None,
        relation: str | None = None,
        object: str | None = None,
        limit: int = 20,
    ) -> list[Relationship]:
        conditions: list[str] = []
        parameters: list[str | int] = []

        if subject:
            conditions.append("subject = ?")
            parameters.append(subject.strip())

        if relation:
            conditions.append("relation = ?")
            parameters.append(relation.strip())

        if object:
            conditions.append("object = ?")
            parameters.append(object.strip())

        where_clause = ""

        if conditions:
            where_clause = (
                "WHERE " + " AND ".join(conditions)
            )

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id,
                    subject,
                    relation,
                    object,
                    confidence,
                    created_at,
                    updated_at
                FROM relationships
                {where_clause}
                ORDER BY confidence DESC, id DESC
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()

        return [
            Relationship(**dict(row))
            for row in rows
        ]

    def search_relationships(
        self,
        query: str,
        limit: int = 12,
    ) -> list[Relationship]:
        """
        Find relationships relevant to a natural-language query.

        This is intentionally lightweight:
        - normalize the query
        - remove common conversational words
        - search subject, relation, and object
        - return a broad candidate set
        - let the main LLM decide final relevance
        """
        query = query.strip().lower()

        if not query:
            return []

        stopwords = {
            "what",
            "is",
            "my",
            "the",
            "a",
            "an",
            "of",
            "to",
            "do",
            "does",
            "did",
            "me",
            "i",
            "am",
            "are",
            "was",
            "were",
            "who",
            "where",
            "when",
            "how",
            "can",
            "you",
            "tell",
            "about",
            "name",
        }

        normalized = re.sub(
            r"[^\w\s]",
            " ",
            query,
        )

        terms = [
            term
            for term in normalized.split()
            if len(term) >= 2
            and term not in stopwords
        ]

        if not terms:
            return []

        conditions: list[str] = []
        parameters: list[str] = []

        for term in terms:
            pattern = f"%{term}%"

            conditions.append(
                """
                (
                    LOWER(subject) LIKE ?
                    OR LOWER(relation) LIKE ?
                    OR LOWER(object) LIKE ?
                )
                """
            )

            parameters.extend(
                [pattern, pattern, pattern]
            )

        where_clause = " OR ".join(
            conditions
        )

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id,
                    subject,
                    relation,
                    object,
                    confidence,
                    created_at,
                    updated_at
                FROM relationships
                WHERE {where_clause}
                ORDER BY confidence DESC, id DESC
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()

        return [
            Relationship(**dict(row))
            for row in rows
        ]    

    def related_entities(
        self,
        entity: str,
        max_hops: int = 2,
        limit: int = 20,
    ) -> list[Relationship]:
        """
        Find relationships connected to an entity.

        Performs a lightweight bounded graph traversal
        directly over the SQLite relationships table.

        This is intentionally small and local:
        no graph database is required.
        """
        entity = entity.strip()

        if not entity:
            return []

        max_hops = max(1, min(max_hops, 4))

        visited_entities = {
            entity.lower()
        }

        frontier = {
            entity.lower()
        }

        discovered: list[Relationship] = []
        seen_relationships: set[tuple[str, str, str]] = set()

        for _ in range(max_hops):
            if not frontier:
                break

            conditions: list[str] = []
            parameters: list[str] = []

            for current in frontier:
                conditions.append(
                    "(LOWER(subject) = ? OR LOWER(object) = ?)"
                )
                parameters.extend(
                    [current, current]
                )

            where_clause = " OR ".join(
                conditions
            )

            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT
                        id,
                        subject,
                        relation,
                        object,
                        confidence,
                        created_at,
                        updated_at
                    FROM relationships
                    WHERE {where_clause}
                    ORDER BY confidence DESC, id DESC
                    LIMIT ?
                    """,
                    (*parameters, limit),
                ).fetchall()

            next_frontier: set[str] = set()

            for row in rows:
                relationship = Relationship(
                    **dict(row)
                )

                relationship_key = (
                    relationship.subject.lower(),
                    relationship.relation.lower(),
                    relationship.object.lower(),
                )

                if relationship_key in seen_relationships:
                    continue

                seen_relationships.add(
                    relationship_key
                )

                discovered.append(
                    relationship
                )

                subject = relationship.subject.lower()
                object_ = relationship.object.lower()

                if subject not in visited_entities:
                    visited_entities.add(subject)
                    next_frontier.add(subject)

                if object_ not in visited_entities:
                    visited_entities.add(object_)
                    next_frontier.add(object_)

            frontier = next_frontier

            if len(discovered) >= limit:
                break

        discovered.sort(
            key=lambda item: (
                item.confidence,
                item.id,
            ),
            reverse=True,
        )

        return discovered[:limit]        

    def save_consolidation(
        self,
        result,
        *,
        session_id: int | None = None,
    ) -> None:
        """Persist consolidated memories and the optional session episode.

        ``session_id`` is accepted because the runtime already owns a durable
        session identifier. Keeping this method backward-compatible while
        recording that source fixes shutdown persistence without introducing a
        second memory lifecycle API.
        """
        for memory in result.memories:
            if hasattr(self, "remember_rich"):
                self.remember_rich(
                    kind=memory.category,
                    scope="user",
                    subject="user",
                    key=memory.key,
                    value=memory.value,
                    statement=memory.statement,
                    confidence=memory.confidence,
                    source_session_id=session_id,
                )
            else:
                self.remember(
                    memory.category,
                    memory.key,
                    memory.value,
                    memory.statement,
                    memory.confidence,
                )

        for relationship in result.relationships:
            self.remember_relationship(
                subject=relationship.subject,
                relation=relationship.relation,
                object=relationship.object,
                confidence=relationship.confidence,
            )

        summary = str(getattr(result, "summary", "") or "").strip()
        if summary and session_id is not None:
            self.save_episode(
                title="ADVI session",
                summary=summary,
                session_id=session_id,
            )

    def latest_session(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    started_at,
                    ended_at,
                    summary
                FROM sessions
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()

        return dict(row) if row else None