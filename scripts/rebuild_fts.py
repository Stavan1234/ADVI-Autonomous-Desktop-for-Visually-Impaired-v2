import sqlite3

database = "runtime/memory/advi_memory.db"

connection = sqlite3.connect(database)

connection.execute(
    """
    INSERT INTO memories_fts(memories_fts)
    VALUES ('rebuild')
    """
)

connection.commit()

rows = connection.execute(
    """
    SELECT rowid, key, value
    FROM memories_fts
    WHERE memories_fts MATCH ?
    """,
    ("name",),
).fetchall()

print(rows)

connection.close()