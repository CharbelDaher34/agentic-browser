# SPDX-License-Identifier: Apache-2.0
"""MemoryStore and SqliteStore must behave identically across the 11-method core
Store surface. Exercising both with the same inputs guarantees neither impl is
missing a method or diverging (the make-or-break gate for headless embedding).
Keys are NOT a store concern (they live in CoreConfig), so they're not exercised here."""

from __future__ import annotations

from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, UserPromptPart

from agenticbrowser.models import Action, ActionKind, ActionResult, PageObservation, StepRecord
from agenticbrowser.stores import MemoryStore, SqliteStore


def _step() -> StepRecord:
    obs = PageObservation(
        url="https://example.com", title="Example", elements=[],
        text_digest="hello", fingerprint="fp",
    )
    return StepRecord(
        chat_id="c1", session_id="s1", idx=1,
        action=Action(ActionKind.NAVIGATE, url="https://example.com"),
        result=ActionResult(ok=True, changed=True, observation=obs),
        screenshot_uri="mem://1.png",
    )


async def _exercise(store, msgs) -> dict:
    """Run the full core Store surface and return every read for comparison."""
    await store.upsert_session("s1", "local")
    await store.save_storage_state("s1", {"cookies": []}, last_url="https://example.com")
    await store.save_bb_session_id("s1", "bb123")
    await store.save_messages("c1", msgs)
    await store.insert_step(_step())
    return {
        "storage_state": await store.load_storage_state("s1"),
        "last_url": await store.load_last_url("s1"),
        "bb_id": await store.load_bb_session_id("s1"),
        "session": await store.get_session("s1"),
        "max_idx": await store.max_step_idx("c1"),
        "messages": ModelMessagesTypeAdapter.dump_json(await store.load_messages("c1")),
        "steps": await store.list_steps("c1"),
    }


async def test_memory_and_sqlite_are_identical(tmp_path):
    mem = MemoryStore()
    sql = SqliteStore(str(tmp_path / "t.db"))
    # build the history ONCE so the embedded timestamp is identical for both stores
    msgs = [ModelRequest(parts=[UserPromptPart(content="hi")])]
    try:
        mem_reads = await _exercise(mem, msgs)
        sql_reads = await _exercise(sql, msgs)
        assert mem_reads == sql_reads
        # spot-check a few concrete values so a both-wrong bug can't pass
        assert sql_reads["max_idx"] == 1
        assert sql_reads["session"] == {"session_id": "s1", "provider": "local"}
        assert sql_reads["bb_id"] == "bb123"
        assert sql_reads["steps"][0]["url"] == "https://example.com"
    finally:
        await sql.close()


async def test_list_steps_since(tmp_path):
    """`since` returns only steps with idx > since (incremental fetch)."""
    def step(i: int) -> StepRecord:
        obs = PageObservation(url=f"https://x/{i}", title="", elements=[], text_digest="", fingerprint="")
        return StepRecord(
            chat_id="c", session_id="s", idx=i,
            action=Action(ActionKind.NAVIGATE, url=f"https://x/{i}"),
            result=ActionResult(ok=True, changed=True, observation=obs), screenshot_uri=None,
        )

    sql = SqliteStore(str(tmp_path / "since.db"))
    try:
        for st in (MemoryStore(), sql):
            await st.insert_step(step(1))
            await st.insert_step(step(2))
            assert [s["idx"] for s in await st.list_steps("c")] == [1, 2]
            assert [s["idx"] for s in await st.list_steps("c", since=1)] == [2]
    finally:
        await sql.close()


async def test_empty_reads_match(tmp_path):
    mem = MemoryStore()
    sql = SqliteStore(str(tmp_path / "e.db"))
    try:
        assert await mem.load_storage_state("nope") is None
        assert await sql.load_storage_state("nope") is None
        assert await mem.max_step_idx("nope") == await sql.max_step_idx("nope") == 0
        assert await mem.load_messages("nope") == await sql.load_messages("nope") == []
        assert await mem.get_session("nope") is await sql.get_session("nope") is None
    finally:
        await sql.close()
