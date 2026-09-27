import pytest

from relay.kb import MAX_DOC_CHARS, DocRef, build_fts_query, chunk_text


def test_chunk_text_keeps_paragraphs_and_respects_limit():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 50 for i in range(10))
    chunks = chunk_text(text, max_chars=600)
    assert all(len(c) <= 600 for c in chunks)
    assert "".join(chunks).count("Paragraph") == 10
    assert len(chunks) > 1


def test_chunk_text_splits_oversized_paragraph_and_sentence():
    text = "Short sentence. " * 100 + "x" * 3000
    chunks = chunk_text(text, max_chars=500)
    assert all(len(c) <= 500 for c in chunks)
    assert sum(c.count("x") for c in chunks) == 3000


def test_chunk_text_empty():
    assert chunk_text("  \n\n ") == []


@pytest.mark.parametrize(
    "question",
    ['how do I "reset" my password?', "NEAR(a b) OR -x AND y:z", "col:foo* ^bar", "it's (broken)"],
)
def test_fts_query_quotes_every_term(question):
    query = build_fts_query(question)
    assert query is not None
    for term in query.split(" OR "):
        assert term.startswith('"') and term.endswith('"') and '"' not in term[1:-1]


def test_fts_query_drops_stopwords_and_dupes():
    assert build_fts_query("How do I get a refund refund?") == '"get" OR "refund"'
    assert build_fts_query("hi, thanks!") is None


async def test_search_ranks_relevant_doc_and_is_guild_scoped(kb):
    await kb.add_doc(1, "Refund policy", "Refunds are available within 14 days of purchase.", "manual")
    await kb.add_doc(1, "Installing", "Download the installer and run setup.exe on Windows.", "manual")
    await kb.add_doc(2, "Other server refunds", "Refunds are never available here.", "manual")

    hits = await kb.search(1, "Can I get a refund after purchase?")
    assert hits and hits[0].title == "Refund policy"
    assert all(h.title != "Other server refunds" for h in hits)
    assert await kb.search(1, "banana smoothie") == []


async def test_search_survives_hostile_syntax(kb):
    await kb.add_doc(1, "Doc", "Some text about tokens.", "manual")
    assert await kb.search(1, '") OR 1=1 -- NEAR(tokens') is not None


async def test_remove_doc_removes_chunks(kb):
    doc_id, _ = await kb.add_doc(1, "Refunds", "Refunds within 14 days.", "manual")
    assert not await kb.remove_doc(2, doc_id)  # other guild can't delete it
    assert await kb.remove_doc(1, doc_id)
    assert await kb.search(1, "refunds") == []
    assert await kb.list_docs(1) == []


async def test_add_doc_validation(kb):
    with pytest.raises(ValueError):
        await kb.add_doc(1, "Empty", "   ", "manual")
    with pytest.raises(ValueError):
        await kb.add_doc(1, "Huge", "x" * (MAX_DOC_CHARS + 1), "manual")


async def test_doc_refs(kb):
    a, _ = await kb.add_doc(1, "A", "alpha text", "manual")
    b, _ = await kb.add_doc(1, "B", "beta text", "thread:9", url="https://discord.com/channels/1/9")
    refs = await kb.doc_refs(1, [a, b])
    assert refs[a] == DocRef("A", None) and refs[b] == DocRef("B", "https://discord.com/channels/1/9")
    assert await kb.doc_refs(2, [a]) == {}


async def test_replace_source_swaps_docs_atomically(kb):
    await kb.add_doc(1, "Old page", "old refund text", "site:https://x.dev/")
    await kb.add_doc(1, "Manual", "manual refund text", "manual")
    added = await kb.replace_source(
        1, "site:https://x.dev/", [("New A", "new refund text", "https://x.dev/a"), ("Empty", "  ", None)]
    )
    assert added == 1
    titles = sorted(d.title for d in await kb.list_docs(1))
    assert titles == ["Manual", "New A"]
    assert all(c.title != "Old page" for c in await kb.search(1, "refund"))
    assert await kb.has_source(1, "site:https://x.dev/") and not await kb.has_source(2, "site:https://x.dev/")
    assert await kb.remove_source(1, "site:https://x.dev/") == 1
    assert await kb.search(1, "new") == []


async def test_extra_terms_find_paraphrased_doc(kb):
    await kb.add_doc(1, "Refund policy", "Refunds are available within 14 days.", "manual")
    assert await kb.search(1, "can I get my money back?") == []
    hits = await kb.search(1, "can I get my money back?", extra_terms=["refund"])
    assert hits and hits[0].title == "Refund policy"


async def test_count_new_docs(kb):
    await kb.add_doc(1, "A", "alpha", "learned:5")
    await kb.add_doc(1, "B", "beta", "manual")
    assert await kb.count_new_docs(1, "learned:", "2000-01-01") == 1
    assert await kb.count_new_docs(1, "learned:", "2999-01-01") == 0
