# SPDX-License-Identifier: Apache-2.0
"""BrowserAgent SDK: config wiring + store selection + approval normalization
(browser-free), plus a real headless end-to-end run driven by a scripted
FunctionModel (no LLM key, no network) against a local HTML file."""

from __future__ import annotations

import asyncio

import pytest

from agenticbrowser import Approval, BrowserAgent, MemoryStore, SqliteStore
from agenticbrowser.sdk import ApprovalRequest, _normalize


# ----------------------------- browser-free units ---------------------------
def test_config_wiring_defaults():
    ab = BrowserAgent(keys={"anthropic": "sk-x"})
    assert ab._cfg.provider_keys == {"anthropic": "sk-x"}  # keys= becomes the process's provider_keys
    assert ab._cfg.max_subagent_depth == 0           # subagents off by default


def test_config_wiring_options():
    ab = BrowserAgent(
        keys={"google": "k"}, backend="browserbase",
        browserbase={"api_key": "bb", "project_id": "proj"},
        subagents=True, max_concurrent_subagents=3, max_tabs=8,
        max_steps=20, max_tokens=50_000,
    )
    c = ab._cfg
    assert c.browser_provider == "browserbase"
    assert c.browserbase_api_key == "bb" and c.browserbase_project_id == "proj"
    assert c.max_subagent_depth == 1 and c.max_concurrent_subagents == 3 and c.max_tabs == 8
    assert c.max_steps == 20 and c.max_tokens == 50_000


def test_store_selection():
    assert isinstance(BrowserAgent(keys={})._make_store(), MemoryStore)
    sql = BrowserAgent(keys={}, persist="sqlite:///runs.db")._make_store()
    assert isinstance(sql, SqliteStore) and sql._path == "runs.db"
    custom = MemoryStore()
    assert BrowserAgent(keys={}, persist=custom)._make_store() is custom


def test_keys_go_into_config_not_store():
    ab = BrowserAgent(keys={"anthropic": "sk-x"})
    # keys live on the config (the process's provider_keys), not on the store
    assert ab._cfg.provider_keys == {"anthropic": "sk-x"}
    store = ab._make_store()
    assert not hasattr(store, "_keys")


def test_approval_normalization():
    assert _normalize(True).approved is True
    assert _normalize(Approval.allow()).approved is True
    d = _normalize("nope")
    assert d.approved is False and d.reason == "nope"
    assert _normalize(False).approved is False


async def test_run_before_open_raises():
    ab = BrowserAgent(keys={})
    with pytest.raises(RuntimeError):
        await ab.run("do something")


# ----------------------------- real headless E2E ----------------------------
# `run_stream_events` needs a STREAMING model, so we script a FunctionModel with a
# `stream_function`. Each step is ("tool", name, args) or ("text", final_answer).
def _scripted_model(steps):
    import json

    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    state = {"i": 0}

    async def stream_fn(messages, info):
        i = state["i"]
        state["i"] += 1
        step = steps[min(i, len(steps) - 1)]
        if step[0] == "tool":
            yield {0: DeltaToolCall(name=step[1], json_args=json.dumps(step[2]), tool_call_id=f"c{i}")}
        else:
            yield step[1]

    return FunctionModel(stream_function=stream_fn), state


async def test_e2e_navigate_and_finish(tmp_path):
    """Drive a real Chromium to a local file and finish — scripted, no LLM call.
    Skips cleanly if a browser can't be launched in this environment."""
    from agenticbrowser.agent import agent as orchestrator

    page = tmp_path / "page.html"
    page.write_text("<html><head><title>AB</title></head><body><h1>hi</h1></body></html>")
    url = page.as_uri()
    model, state = _scripted_model([("tool", "navigate", {"url": url}), ("text", "DONE")])

    try:
        with orchestrator.override(model=model):
            async with BrowserAgent(keys={"anthropic": "sk-test"}, headless=True) as ab:
                result = await ab.run("go to the test page")
    except RuntimeError as exc:
        if "Failed to open a browser" in str(exc):
            pytest.skip(f"browser unavailable in this environment: {exc}")
        raise

    assert result.output == "DONE"
    assert state["i"] == 2                                    # navigate, then finish
    assert any(str(s["url"]).startswith("file://") for s in result.steps)
    assert result.usage.get("steps", 0) >= 1                  # at least the navigate step
    # usage must carry REAL token/request counts — `result.usage` is a property in
    # pydantic-ai v2, and calling it `()` silently zeroed these (regression guard).
    assert result.usage.get("requests", 0) >= 1
    assert result.usage.get("total_tokens", 0) > 0
    assert result.usage["total_tokens"] == (
        result.usage["input_tokens"] + result.usage["output_tokens"]
    )


async def test_e2e_approval_auto_denied(tmp_path):
    """With no approve= handler, a destructive action is auto-denied (fail-safe):
    the run pauses for approval and the action is NOT performed."""
    from agenticbrowser.agent import agent as orchestrator

    page = tmp_path / "p.html"
    page.write_text("<html><body><button>Pay now</button></body></html>")
    url = page.as_uri()
    model, state = _scripted_model([
        ("tool", "navigate", {"url": url}),
        ("tool", "click_at", {"x": 5, "y": 5, "label": "Pay now"}),  # destructive -> gate
        ("text", "STOPPED"),
    ])

    try:
        with orchestrator.override(model=model):
            async with BrowserAgent(keys={"anthropic": "sk-test"}, headless=True) as ab:
                result = await ab.run("try to pay")
    except RuntimeError as exc:
        if "Failed to open a browser" in str(exc):
            pytest.skip(f"browser unavailable in this environment: {exc}")
        raise

    assert result.output == "STOPPED"
    assert len(result.approvals) >= 1     # the destructive click paused for approval


async def test_e2e_unlabeled_coordinate_click_still_gates(tmp_path):
    """A coordinate click on a real destructive button with NO label must still pause
    for approval. The gate now classifies the element actually under (x, y) via a DOM
    hit-test, so it no longer depends on the model-supplied label (which defaulted '' and
    used to let destructive coordinate clicks through)."""
    from agenticbrowser.agent import agent as orchestrator

    page = tmp_path / "p.html"
    page.write_text(
        "<html><body style='margin:0'>"
        "<form action='/checkout'>"
        "<button type='submit' style='position:absolute;left:0;top:0;width:300px;height:60px'>"
        "Place Order</button></form></body></html>"
    )
    url = page.as_uri()
    model, _ = _scripted_model([
        ("tool", "navigate", {"url": url}),
        ("tool", "click_at", {"x": 150, "y": 30}),   # NO label — lands on "Place Order"
        ("text", "STOPPED"),
    ])

    try:
        with orchestrator.override(model=model):
            async with BrowserAgent(keys={"anthropic": "sk-test"}, headless=True) as ab:
                result = await ab.run("buy it")
    except RuntimeError as exc:
        if "Failed to open a browser" in str(exc):
            pytest.skip(f"browser unavailable in this environment: {exc}")
        raise

    assert result.output == "STOPPED"
    assert len(result.approvals) >= 1     # paused despite the model passing no label


async def test_e2e_step_records_readable_target(tmp_path):
    """A recorded step carries the human-readable name of the element acted on, so the
    audit reads Clicked "Read more" instead of an opaque ref or raw coordinates."""
    from agenticbrowser.agent import agent as orchestrator

    page = tmp_path / "p.html"
    page.write_text(
        "<html><body style='margin:0'>"
        "<a href='#x' style='position:absolute;left:0;top:0;width:300px;height:50px;"
        "display:block'>Read more</a></body></html>"
    )
    url = page.as_uri()
    model, _ = _scripted_model([
        ("tool", "navigate", {"url": url}),
        ("tool", "click_at", {"x": 150, "y": 25}),   # benign -> executes -> records a step
        ("text", "DONE"),
    ])

    try:
        with orchestrator.override(model=model):
            async with BrowserAgent(keys={"anthropic": "sk-test"}, headless=True) as ab:
                result = await ab.run("open it")
    except RuntimeError as exc:
        if "Failed to open a browser" in str(exc):
            pytest.skip(f"browser unavailable in this environment: {exc}")
        raise

    assert result.output == "DONE"
    clicks = [s for s in result.steps if s["action"]["kind"] == "click_at"]
    assert clicks, "expected a recorded click_at step"
    assert clicks[0]["action"].get("target") == "Read more"   # the resolved element name


async def test_e2e_raising_approve_handler_does_not_hang(tmp_path):
    """A buggy approve= handler that raises must NOT hang the run (it should deny
    the action, fail-safe, and complete). asyncio.wait_for guards the test."""
    from agenticbrowser.agent import agent as orchestrator

    page = tmp_path / "p.html"
    page.write_text("<html><body><button>Pay now</button></body></html>")
    url = page.as_uri()
    model, _ = _scripted_model([
        ("tool", "navigate", {"url": url}),
        ("tool", "click_at", {"x": 5, "y": 5, "label": "Pay now"}),
        ("text", "STOPPED"),
    ])

    async def boom(req):
        raise ValueError("handler crashed")

    try:
        with orchestrator.override(model=model):
            async with BrowserAgent(keys={"anthropic": "sk-test"}, headless=True, approve=boom) as ab:
                result = await asyncio.wait_for(ab.run("try to pay"), timeout=60)
    except RuntimeError as exc:
        if "Failed to open a browser" in str(exc):
            pytest.skip(f"browser unavailable in this environment: {exc}")
        raise

    assert result.output == "STOPPED"     # denied despite the handler error, no hang
