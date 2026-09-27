"""Per-server knowledge base: chunking, storage, and BM25 retrieval via SQLite FTS5.

FTS5 keeps retrieval free (no embedding API) and fast enough for the size of
a community's docs. Every query is scoped to one guild; a server can never
retrieve another server's chunks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from relay.db import Database, iso, utcnow

MAX_DOC_CHARS = 200_000
CHUNK_CHARS = 1_200
MAX_QUERY_TERMS = 24

# Common words that only add noise to an OR query.
_STOPWORDS = frozenset(
    """
    a an and are as at be but by can do does did for from had has have how i if in into is it its
    me my of on or our so than that the their them then there these they this to was we were what
    when where which who why will with would you your hi hey hello please thanks thank anyone help
    """.split()
)
_WORD = re.compile(r"[\w']+", re.UNICODE)


@dataclass(frozen=True)
class Doc:
    id: int
    title: str
    source: str
    char_count: int
    created_at: str
    url: str | None = None


@dataclass(frozen=True)
class DocRef:
    title: str
    url: str | None


@dataclass(frozen=True)
class Chunk:
    doc_id: int
    title: str
    text: str


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """Split one oversized paragraph on sentence boundaries, hard-cutting any
    single sentence that is still too long."""
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    out: list[str] = []
    buf = ""
    for s in sentences:
        while len(s) > max_chars:
            if buf:
                out.append(buf)
                buf = ""
            out.append(s[:max_chars])
            s = s[max_chars:]
        if buf and len(buf) + 1 + len(s) > max_chars:
            out.append(buf)
            buf = s
        else:
            buf = f"{buf} {s}" if buf else s
    if buf:
        out.append(buf)
    return out


def chunk_text(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Pack paragraphs into chunks of at most max_chars, keeping paragraphs
    whole where possible so each chunk reads as a coherent passage."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        pieces = [p] if len(p) <= max_chars else _split_long(p, max_chars)
        for piece in pieces:
            if buf and len(buf) + 2 + len(piece) > max_chars:
                chunks.append(buf)
                buf = piece
            else:
                buf = f"{buf}\n\n{piece}" if buf else piece
    if buf:
        chunks.append(buf)
    return chunks


def build_fts_query(question: str, extra_terms: tuple[str, ...] | list[str] = ()) -> str | None:
    """Turn free text (plus optional extra keywords, e.g. English terms from
    query expansion) into a safe FTS5 OR-query.

    User text can't be passed to MATCH directly: quotes, colons, `NEAR`,
    `-` etc. are FTS5 syntax and would either error or change the query's
    meaning. Every term is double-quoted, so it's matched literally.
    """
    terms: list[str] = []
    seen: set[str] = set()
    # Expansion keywords go first: they survive the MAX_QUERY_TERMS cap.
    text = " ".join([*extra_terms, question])
    for word in _WORD.findall(text.lower()):
        word = word.strip("'_")
        if len(word) < 2 or word in _STOPWORDS or word in seen:
            continue
        seen.add(word)
        terms.append('"' + word.replace('"', "") + '"')
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return " OR ".join(terms) if terms else None


class KnowledgeBase:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add_doc(
        self, guild_id: int, title: str, text: str, source: str, url: str | None = None, *, commit: bool = True
    ) -> tuple[int, int]:
        """Store a document; returns (doc_id, number_of_chunks)."""
        title = title.strip()[:200] or "Untitled"
        text = text.strip()
        if not text:
            raise ValueError("document is empty")
        if len(text) > MAX_DOC_CHARS:
            raise ValueError(f"document is too long ({len(text):,} chars, max {MAX_DOC_CHARS:,})")
        conn = self.db.conn
        cur = await conn.execute(
            "INSERT INTO kb_docs (guild_id, title, source, char_count, created_at, url) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, title, source, len(text), iso(utcnow()), url),
        )
        doc_id = cur.lastrowid
        chunks = chunk_text(text)
        await conn.executemany(
            "INSERT INTO kb_chunks (text, title, guild_id, doc_id) VALUES (?, ?, ?, ?)",
            [(c, title, guild_id, doc_id) for c in chunks],
        )
        if commit:
            await conn.commit()
        return doc_id, len(chunks)

    async def has_source(self, guild_id: int, source: str) -> bool:
        async with self.db.conn.execute(
            "SELECT 1 FROM kb_docs WHERE guild_id = ? AND source = ? LIMIT 1", (guild_id, source)
        ) as cur:
            return await cur.fetchone() is not None

    async def remove_source(self, guild_id: int, source: str, *, commit: bool = True) -> int:
        """Delete every doc with this exact source; returns how many."""
        conn = self.db.conn
        async with conn.execute("SELECT id FROM kb_docs WHERE guild_id = ? AND source = ?", (guild_id, source)) as cur:
            ids = [r["id"] for r in await cur.fetchall()]
        for doc_id in ids:
            await conn.execute("DELETE FROM kb_chunks WHERE doc_id = ? AND guild_id = ?", (doc_id, guild_id))
        await conn.execute("DELETE FROM kb_docs WHERE guild_id = ? AND source = ?", (guild_id, source))
        if commit:
            await conn.commit()
        return len(ids)

    async def replace_source(self, guild_id: int, source: str, docs: list[tuple[str, str, str | None]]) -> int:
        """Atomically swap all docs from `source` for new (title, text, url)
        docs -- used by website re-syncs and re-indexed threads."""
        conn = self.db.conn
        try:
            await self.remove_source(guild_id, source, commit=False)
            added = 0
            for title, text, url in docs:
                if text.strip():
                    await self.add_doc(guild_id, title, text[:MAX_DOC_CHARS], source, url, commit=False)
                    added += 1
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        return added

    async def count_new_docs(self, guild_id: int, source_prefix: str, since: str) -> int:
        async with self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM kb_docs WHERE guild_id = ? AND source LIKE ? AND created_at >= ?",
            (guild_id, source_prefix + "%", since),
        ) as cur:
            return (await cur.fetchone())["n"]

    async def list_docs(self, guild_id: int) -> list[Doc]:
        async with self.db.conn.execute(
            "SELECT id, title, source, char_count, created_at, url FROM kb_docs WHERE guild_id = ? ORDER BY id",
            (guild_id,),
        ) as cur:
            return [Doc(**dict(r)) for r in await cur.fetchall()]

    async def doc_refs(self, guild_id: int, doc_ids: list[int]) -> dict[int, DocRef]:
        if not doc_ids:
            return {}
        marks = ",".join("?" * len(doc_ids))
        async with self.db.conn.execute(
            f"SELECT id, title, url FROM kb_docs WHERE guild_id = ? AND id IN ({marks})", (guild_id, *doc_ids)
        ) as cur:
            return {r["id"]: DocRef(title=r["title"], url=r["url"]) for r in await cur.fetchall()}

    async def remove_doc(self, guild_id: int, doc_id: int) -> bool:
        conn = self.db.conn
        cur = await conn.execute("DELETE FROM kb_docs WHERE id = ? AND guild_id = ?", (doc_id, guild_id))
        if cur.rowcount == 0:
            return False
        await conn.execute("DELETE FROM kb_chunks WHERE doc_id = ? AND guild_id = ?", (doc_id, guild_id))
        await conn.commit()
        return True

    async def search(
        self, guild_id: int, question: str, k: int = 5, extra_terms: tuple[str, ...] | list[str] = ()
    ) -> list[Chunk]:
        query = build_fts_query(question, extra_terms)
        if query is None:
            return []
        # Titles are weighted 2x: a doc titled "Refunds" is very likely the
        # right source for a refund question even if the body says "money back".
        async with self.db.conn.execute(
            "SELECT doc_id, title, text FROM kb_chunks"
            " WHERE kb_chunks MATCH ? AND guild_id = ?"
            " ORDER BY bm25(kb_chunks, 1.0, 2.0) LIMIT ?",
            (query, guild_id, k),
        ) as cur:
            return [Chunk(doc_id=r["doc_id"], title=r["title"], text=r["text"]) for r in await cur.fetchall()]
