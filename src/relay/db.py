"""SQLite storage: guild config, help channels, knowledge base, questions, usage.

One aiosqlite connection for the whole bot. Schema changes are appended to
MIGRATIONS and tracked with PRAGMA user_version, so an existing database is
upgraded in place on startup.

All timestamps are stored as ISO-8601 UTC strings, which sort correctly as text.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE guild_config (
        guild_id INTEGER PRIMARY KEY,
        staff_role_id INTEGER,
        digest_channel_id INTEGER,
        digest_hour_utc INTEGER NOT NULL DEFAULT 14,
        last_digest_date TEXT,
        last_limit_notice_month TEXT
    );
    CREATE TABLE help_channels (
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, channel_id)
    );
    CREATE TABLE kb_docs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        source TEXT NOT NULL,
        char_count INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX kb_docs_guild ON kb_docs (guild_id);
    CREATE VIRTUAL TABLE kb_chunks USING fts5(
        text, title, guild_id UNINDEXED, doc_id UNINDEXED,
        tokenize = 'porter unicode61'
    );
    CREATE TABLE questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        message_id INTEGER,
        user_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        status TEXT NOT NULL,
        source_doc_ids TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        resolved_at TEXT
    );
    CREATE INDEX questions_guild_created ON questions (guild_id, created_at);
    CREATE TABLE usage (
        guild_id INTEGER NOT NULL,
        month TEXT NOT NULL,
        answers INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, month)
    );
    """,
]

# Question lifecycle. "answered" is the state right after the bot posts; the
# asker (or staff) then moves it to "solved" or "escalated".
ANSWERED = "answered"
DECLINED = "declined"
SOLVED = "solved"
ESCALATED = "escalated"
STATUSES = (ANSWERED, DECLINED, SOLVED, ESCALATED)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class GuildConfig:
    guild_id: int
    staff_role_id: int | None = None
    digest_channel_id: int | None = None
    digest_hour_utc: int = 14
    last_digest_date: str | None = None
    last_limit_notice_month: str | None = None


@dataclass(frozen=True)
class Question:
    id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    user_id: int
    text: str
    status: str
    source_doc_ids: tuple[int, ...]
    created_at: str
    resolved_at: str | None


def _row_to_question(row: aiosqlite.Row) -> Question:
    ids = tuple(int(x) for x in row["source_doc_ids"].split(",") if x)
    return Question(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        user_id=row["user_id"],
        text=row["text"],
        status=row["status"],
        source_doc_ids=ids,
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    @classmethod
    async def open(cls, path: Path | str) -> Database:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        db = cls(conn)
        await db.migrate()
        return db

    async def close(self) -> None:
        await self.conn.close()

    async def migrate(self) -> None:
        async with self.conn.execute("PRAGMA user_version") as cur:
            (version,) = await cur.fetchone()
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            await self.conn.executescript(script)
            await self.conn.execute(f"PRAGMA user_version = {i}")
        await self.conn.commit()

    # --- guild config -------------------------------------------------------

    async def get_guild_config(self, guild_id: int) -> GuildConfig:
        async with self.conn.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return GuildConfig(guild_id=guild_id)
        return GuildConfig(**dict(row))

    async def all_guild_configs(self) -> list[GuildConfig]:
        async with self.conn.execute("SELECT * FROM guild_config") as cur:
            return [GuildConfig(**dict(r)) for r in await cur.fetchall()]

    async def update_guild_config(self, guild_id: int, **fields: object) -> GuildConfig:
        allowed = set(GuildConfig.__dataclass_fields__) - {"guild_id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown guild_config fields: {sorted(unknown)}")
        await self.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            await self.conn.execute(
                f"UPDATE guild_config SET {assignments} WHERE guild_id = ?", (*fields.values(), guild_id)
            )
        await self.conn.commit()
        return await self.get_guild_config(guild_id)

    # --- help channels ------------------------------------------------------

    async def help_channel_ids(self, guild_id: int) -> set[int]:
        async with self.conn.execute("SELECT channel_id FROM help_channels WHERE guild_id = ?", (guild_id,)) as cur:
            return {r["channel_id"] for r in await cur.fetchall()}

    async def all_help_channel_ids(self) -> set[int]:
        async with self.conn.execute("SELECT channel_id FROM help_channels") as cur:
            return {r["channel_id"] for r in await cur.fetchall()}

    async def add_help_channel(self, guild_id: int, channel_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO help_channels (guild_id, channel_id) VALUES (?, ?)", (guild_id, channel_id)
        )
        await self.conn.commit()

    async def remove_help_channel(self, guild_id: int, channel_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM help_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # --- questions ----------------------------------------------------------

    async def record_question(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int | None,
        user_id: int,
        text: str,
        status: str,
        source_doc_ids: Iterable[int] = (),
        now: datetime | None = None,
    ) -> int:
        if status not in STATUSES:
            raise ValueError(f"bad status {status!r}")
        cur = await self.conn.execute(
            "INSERT INTO questions (guild_id, channel_id, message_id, user_id, text, status,"
            " source_doc_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id,
                channel_id,
                message_id,
                user_id,
                text,
                status,
                ",".join(str(i) for i in source_doc_ids),
                iso(now or utcnow()),
            ),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_question(self, question_id: int) -> Question | None:
        async with self.conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_question(row) if row else None

    async def resolve_question(self, question_id: int, status: str, now: datetime | None = None) -> bool:
        """Move an answered question to solved/escalated. Returns False if it was
        already resolved (so a double-click doesn't re-ping staff)."""
        if status not in (SOLVED, ESCALATED):
            raise ValueError(f"bad resolution {status!r}")
        cur = await self.conn.execute(
            "UPDATE questions SET status = ?, resolved_at = ? WHERE id = ? AND status = ?",
            (status, iso(now or utcnow()), question_id, ANSWERED),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def questions_since(self, guild_id: int, since: datetime) -> list[Question]:
        async with self.conn.execute(
            "SELECT * FROM questions WHERE guild_id = ? AND created_at >= ? ORDER BY created_at",
            (guild_id, iso(since)),
        ) as cur:
            return [_row_to_question(r) for r in await cur.fetchall()]

    async def status_counts(self, guild_id: int, since: datetime) -> dict[str, int]:
        async with self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM questions WHERE guild_id = ? AND created_at >= ? GROUP BY status",
            (guild_id, iso(since)),
        ) as cur:
            counts = {s: 0 for s in STATUSES}
            counts.update({r["status"]: r["n"] for r in await cur.fetchall()})
            return counts

    async def purge_questions_before(self, cutoff: datetime) -> int:
        cur = await self.conn.execute("DELETE FROM questions WHERE created_at < ?", (iso(cutoff),))
        await self.conn.commit()
        return cur.rowcount

    # --- usage metering -----------------------------------------------------

    async def usage_for(self, guild_id: int, month: str) -> int:
        async with self.conn.execute(
            "SELECT answers FROM usage WHERE guild_id = ? AND month = ?", (guild_id, month)
        ) as cur:
            row = await cur.fetchone()
        return row["answers"] if row else 0

    async def increment_usage(self, guild_id: int, month: str) -> int:
        await self.conn.execute(
            "INSERT INTO usage (guild_id, month, answers) VALUES (?, ?, 1)"
            " ON CONFLICT (guild_id, month) DO UPDATE SET answers = answers + 1",
            (guild_id, month),
        )
        await self.conn.commit()
        return await self.usage_for(guild_id, month)
