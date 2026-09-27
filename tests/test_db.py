from datetime import UTC, datetime, timedelta

import pytest

from relay.db import ANSWERED, DECLINED, ESCALATED, MIGRATIONS, SOLVED, Database


async def test_migrations_set_user_version(db):
    async with db.conn.execute("PRAGMA user_version") as cur:
        (version,) = await cur.fetchone()
    assert version == len(MIGRATIONS)


async def test_reopening_existing_file_does_not_remigrate(tmp_path):
    path = tmp_path / "relay.db"
    first = await Database.open(path)
    await first.add_help_channel(1, 10)
    await first.close()
    second = await Database.open(path)
    assert await second.help_channel_ids(1) == {10}
    await second.close()


async def test_guild_config_defaults_and_update(db):
    cfg = await db.get_guild_config(5)
    assert cfg.staff_role_id is None and cfg.digest_hour_utc == 14
    cfg = await db.update_guild_config(5, staff_role_id=99, digest_hour_utc=3)
    assert (cfg.staff_role_id, cfg.digest_hour_utc) == (99, 3)
    with pytest.raises(ValueError):
        await db.update_guild_config(5, bogus=1)


async def test_help_channels_are_per_guild(db):
    await db.add_help_channel(1, 10)
    await db.add_help_channel(1, 10)  # idempotent
    await db.add_help_channel(2, 20)
    assert await db.help_channel_ids(1) == {10}
    assert await db.all_help_channel_ids() == {10, 20}
    assert await db.remove_help_channel(1, 10)
    assert not await db.remove_help_channel(1, 10)


async def test_resolve_question_only_once(db):
    qid = await db.record_question(
        guild_id=1, channel_id=2, message_id=3, user_id=4, text="q", status=ANSWERED, source_doc_ids=[7, 8]
    )
    q = await db.get_question(qid)
    assert q.source_doc_ids == (7, 8)
    assert await db.resolve_question(qid, ESCALATED)
    assert not await db.resolve_question(qid, SOLVED)  # double-click doesn't re-resolve
    assert (await db.get_question(qid)).status == ESCALATED


async def test_declined_questions_cannot_be_resolved(db):
    qid = await db.record_question(guild_id=1, channel_id=2, message_id=None, user_id=4, text="q", status=DECLINED)
    assert not await db.resolve_question(qid, SOLVED)


async def test_status_counts_and_purge(db):
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    old = now - timedelta(days=40)
    for status, when in [(ANSWERED, now), (DECLINED, now), (DECLINED, now), (ANSWERED, old)]:
        await db.record_question(
            guild_id=1, channel_id=2, message_id=None, user_id=4, text="q", status=status, now=when
        )
    counts = await db.status_counts(1, now - timedelta(days=30))
    assert counts == {ANSWERED: 1, DECLINED: 2, SOLVED: 0, ESCALATED: 0}
    assert await db.purge_questions_before(now - timedelta(days=30)) == 1
    assert len(await db.questions_since(1, old - timedelta(days=1))) == 3


async def test_usage_increments_per_month(db):
    assert await db.usage_for(1, "2026-09") == 0
    await db.increment_usage(1, "2026-09")
    assert await db.increment_usage(1, "2026-09") == 2
    assert await db.usage_for(1, "2026-10") == 0
