# from aiohttp import client_middleware_digest_auth
# from litellm.proxy.pass_through_endpoints.llm_provider_handlers import assembly_passthrough_logging_handler
from __future__ import annotations

from dataclasses import dataclass
import re
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

@dataclass(frozen=True)
class MemoryHistory:
    id: int
    category: str
    key: str
    value: str
    statement: str
    confidence: float
    recorded_at: str


class LongTermMemory:
    """Persistent memory backed by SQLite."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path
        )
        connection.row_factory = sqlite3.Row
        return connection

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
                INSERT INTO memories_fts(memories_fts)
                VALUES ('rebuild')
                """
            )

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
                    updated_at
                FROM memories
                ORDER BY updated_at DESC
                """
            ).fetchall()

        return [
            Memory(**dict(row))
            for row in rows
        ]

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
                    updated_at
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
                    updated_at
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
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM memories
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
    ) -> None:
        for memory in result.memories:
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