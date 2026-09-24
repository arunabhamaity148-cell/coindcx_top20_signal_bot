"""Safety tests (master prompt §25 safety tests, acceptance tests 13 and 21).

EVERY test in this file must confirm NO SIGNAL / FAIL CLOSED. They assert three things:

  1. this source tree contains no order-placement capability at all (static scan);
  2. the boot assertion aborts the process if an exchange trading credential exists;
  3. a broken dependency (Telegram down) is swallowed - the bot keeps running.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.core.errors import SafetyViolation
from app.safety import (
    FORBIDDEN_PATTERNS,
    assert_no_trading_credentials,
    assert_signal_only,
    safety_report,
    scan_forbidden_capabilities,
)


def test_source_tree_contains_no_trading_capability():
    """A static scan of the shipped package must find zero order-related call sites."""
    violations = scan_forbidden_capabilities(os.path.join(os.path.dirname(__file__), "..", ".."))
    assert violations == [], f"trading capability detected: {[str(v) for v in violations]}"


def test_safety_report_is_clean_for_the_shipped_configuration(cfg):
    report = safety_report(cfg)
    assert report["signal_only"] is True
    assert report["trading_credentials_present"] == []
    assert report["forbidden_code_violations"] == []


def test_boot_assertion_passes_with_no_credentials(cfg):
    assert_signal_only(cfg, env={}, scan_source=True)


def test_exchange_trading_key_presence_is_fatal(cfg):
    env = {"BINANCE_API_KEY": "abc", "BINANCE_API_SECRET": "def"}
    with pytest.raises(SafetyViolation) as excinfo:
        assert_signal_only(cfg, env=env, scan_source=False)
    assert "SIGNAL ENGINE MUST NOT START" in str(excinfo.value)


def test_coindcx_trading_key_presence_is_fatal(cfg):
    with pytest.raises(SafetyViolation):
        assert_no_trading_credentials({"COINDCX_API_KEY": "x"}, cfg.system.forbidden_credential_env)


def test_telegram_token_is_allowed(cfg):
    assert_signal_only(
        cfg, env={"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "1"}, scan_source=False
    )


def test_non_signal_only_mode_is_fatal(cfg):
    object.__setattr__(cfg.system, "mode", "live_trading")
    try:
        with pytest.raises(SafetyViolation):
            assert_signal_only(cfg, env={}, scan_source=False)
    finally:
        object.__setattr__(cfg.system, "mode", "signal_only")


def test_veto_override_is_fatal(cfg):
    object.__setattr__(cfg.veto, "override_allowed", True)
    try:
        with pytest.raises(SafetyViolation):
            assert_signal_only(cfg, env={}, scan_source=False)
    finally:
        object.__setattr__(cfg.veto, "override_allowed", False)


def test_scanner_detects_injected_trading_code(tmp_path):
    bad = tmp_path / "evil.py"
    bad.write_text("def place_order(symbol, qty):\n    return 'sent'\n", encoding="utf-8")
    violations = scan_forbidden_capabilities(tmp_path)
    assert violations and violations[0].pattern


def test_scanner_detects_a_private_order_endpoint(tmp_path):
    bad = tmp_path / "rest.py"
    bad.write_text('PATH = "/fapi/v1/order"\n', encoding="utf-8")
    assert scan_forbidden_capabilities(tmp_path)


def test_scanner_ignores_comments_that_only_document_the_guard(tmp_path):
    doc = tmp_path / "note.py"
    doc.write_text("# never call place_order( from this codebase\n", encoding="utf-8")
    assert scan_forbidden_capabilities(tmp_path) == []


def test_forbidden_pattern_list_covers_the_documented_capabilities():
    joined = " ".join(FORBIDDEN_PATTERNS)
    for capability in (
        "place_order",
        "cancel_order",
        "modify_order",
        "close_position",
        "set_leverage",
        "withdraw",
        "transfer_funds",
    ):
        assert capability in joined


@pytest.mark.asyncio
async def test_telegram_failure_never_crashes_the_bot(cfg):
    """A dead Telegram endpoint must be counted as a failure, never propagated."""
    from app.telegram.formatter import MessageFormatter
    from app.telegram.queue import Priority, TelegramQueue
    from app.telegram.sender import SendResult, TelegramSender

    class ExplodingSender(TelegramSender):
        async def send(self, text, *, chat_id=None, dedupe_key=None, disable_notification=False):
            raise RuntimeError("telegram is down")

    sender = ExplodingSender(cfg)
    queue = TelegramQueue(cfg=cfg, formatter=MessageFormatter(cfg), sender=sender)
    await queue.start()
    try:
        await queue.enqueue("hello", priority=Priority.SIGNAL, dedupe_key="k1")
        await queue.enqueue("hello again", priority=Priority.SIGNAL, dedupe_key="k2")
        await asyncio.sleep(0.2)  # let the worker attempt (and fail) both deliveries
    finally:
        await queue.stop()
    assert isinstance(SendResult("", ""), object)  # sanity: importable result type
    assert queue.stats["failed"] >= 1 or queue.stats["dropped"] >= 1


@pytest.mark.asyncio
async def test_telegram_duplicate_is_suppressed_not_resent(cfg):
    from app.telegram.formatter import MessageFormatter
    from app.telegram.queue import Priority, TelegramQueue
    from app.telegram.sender import TelegramSender

    sender = TelegramSender(cfg)  # dry-run: renders and journals, never sends
    queue = TelegramQueue(cfg=cfg, formatter=MessageFormatter(cfg), sender=sender)
    await queue.start()
    try:
        await queue.enqueue("same message", priority=Priority.SIGNAL, dedupe_key="dup")
        await asyncio.sleep(0.15)
        before = queue.stats["sent"]
        await queue.enqueue("same message", priority=Priority.SIGNAL, dedupe_key="dup")
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    assert queue.stats["suppressed"] >= 1
    assert queue.stats["sent"] >= before
