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
    # 2: multi-tier billing -- top-up credits, idempotent entitlement
    # redemption, and per-call token logging for cost reports
    """
    CREATE TABLE credits (
        guild_id INTEGER PRIMARY KEY,
        balance INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE processed_entitlements (
        entitlement_id INTEGER PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        answers INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE llm_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        tier TEXT NOT NULL,
        llm TEXT NOT NULL,
        purpose TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX llm_calls_created ON llm_calls (created_at);
    """,
    # 3: learning from staff / solved threads, and website sync
    """
    ALTER TABLE questions ADD COLUMN thread_id INTEGER;
    CREATE INDEX questions_thread ON questions (thread_id);
    CREATE INDEX questions_message ON questions (message_id);
    ALTER TABLE kb_docs ADD COLUMN url TEXT;
    CREATE INDEX kb_docs_source ON kb_docs (guild_id, source);
    CREATE TABLE kb_sites (
        guild_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        pages INTEGER NOT NULL DEFAULT 0,
        last_synced_at TEXT,
        PRIMARY KEY (guild_id, url)
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
    thread_id: int | None = None


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
        thread_id=row["thread_id"],
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
        thread_id: int | None = None,
        now: datetime | None = None,
    ) -> int:
        if status not in STATUSES:
            raise ValueError(f"bad status {status!r}")
        cur = await self.conn.execute(
            "INSERT INTO questions (guild_id, channel_id, message_id, user_id, text, status,"
            " source_doc_ids, thread_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id,
                channel_id,
                message_id,
                user_id,
                text,
                status,
                ",".join(str(i) for i in source_doc_ids),
                thread_id,
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

    async def set_question_thread(self, question_id: int, thread_id: int) -> None:
        await self.conn.execute("UPDATE questions SET thread_id = ? WHERE id = ?", (thread_id, question_id))
        await self.conn.commit()

    async def find_unanswered_question(
        self, guild_id: int, *, thread_id: int | None = None, message_id: int | None = None
    ) -> Question | None:
        """The most recent question Relay couldn't close (declined or handed to
        staff) that lives in thread_id or was posted as message_id."""
        if thread_id is None and message_id is None:
            return None
        async with self.conn.execute(
            "SELECT * FROM questions WHERE guild_id = ? AND status IN (?, ?)"
            " AND (thread_id = ? OR message_id = ?) ORDER BY id DESC LIMIT 1",
            (guild_id, DECLINED, ESCALATED, thread_id, message_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_question(row) if row else None

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

    # --- top-up credits ----------------------------------------------------

    async def credit_balance(self, guild_id: int) -> int:
        async with self.conn.execute("SELECT balance FROM credits WHERE guild_id = ?", (guild_id,)) as cur:
            row = await cur.fetchone()
        return row["balance"] if row else 0

    async def redeem_entitlement(self, entitlement_id: int, guild_id: int, answers: int, now: datetime) -> bool:
        """Add a top-up's answers to a guild exactly once per entitlement.
        Returns False if this entitlement was already redeemed."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO processed_entitlements (entitlement_id, guild_id, answers, created_at)"
            " VALUES (?, ?, ?, ?)",
            (entitlement_id, guild_id, answers, iso(now)),
        )
        if cur.rowcount == 0:
            await self.conn.commit()
            return False
        await self.conn.execute(
            "INSERT INTO credits (guild_id, balance) VALUES (?, ?)"
            " ON CONFLICT (guild_id) DO UPDATE SET balance = balance + excluded.balance",
            (guild_id, answers),
        )
        await self.conn.commit()
        return True

    async def spend_credit(self, guild_id: int) -> bool:
        cur = await self.conn.execute(
            "UPDATE credits SET balance = balance - 1 WHERE guild_id = ? AND balance > 0", (guild_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # --- AI call log -------------------------------------------------------

    async def log_llm_call(
        self,
        *,
        guild_id: int,
        tier: str,
        llm: str,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        now: datetime | None = None,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO llm_calls (guild_id, tier, llm, purpose, input_tokens, output_tokens, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, tier, llm, purpose, input_tokens, output_tokens, iso(now or utcnow())),
        )
        await self.conn.commit()

    async def llm_usage_since(self, since: datetime) -> list[dict]:
        """Per (tier, llm): calls, distinct guilds, and token totals."""
        async with self.conn.execute(
            "SELECT tier, llm, COUNT(*) AS calls, COUNT(DISTINCT guild_id) AS guilds,"
            " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens"
            " FROM llm_calls WHERE created_at >= ? GROUP BY tier, llm ORDER BY tier, llm",
            (iso(since),),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def purge_llm_calls_before(self, cutoff: datetime) -> int:
        cur = await self.conn.execute("DELETE FROM llm_calls WHERE created_at < ?", (iso(cutoff),))
        await self.conn.commit()
        return cur.rowcount

    # --- synced websites ---------------------------------------------------

    async def upsert_site(self, guild_id: int, url: str, pages: int, now: datetime) -> None:
        await self.conn.execute(
            "INSERT INTO kb_sites (guild_id, url, pages, last_synced_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (guild_id, url) DO UPDATE SET pages = excluded.pages,"
            " last_synced_at = excluded.last_synced_at",
            (guild_id, url, pages, iso(now)),
        )
        await self.conn.commit()

    async def sites(self, guild_id: int | None = None) -> list[dict]:
        if guild_id is None:
            query, args = "SELECT * FROM kb_sites ORDER BY guild_id, url", ()
        else:
            query, args = "SELECT * FROM kb_sites WHERE guild_id = ? ORDER BY url", (guild_id,)
        async with self.conn.execute(query, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def remove_site(self, guild_id: int, url: str) -> bool:
        cur = await self.conn.execute("DELETE FROM kb_sites WHERE guild_id = ? AND url = ?", (guild_id, url))
        await self.conn.commit()
        return cur.rowcount > 0
