"""Rate-limited Telegram sender.

Delivery limits (VERIFIED from the Telegram FAQ, FINAL_DELIVERABLE §T):
  1 msg/sec per chat · 20 msgs/min per group · ~30 msgs/sec free bulk;
  paid broadcasts up to 1000/s require >= 100,000 Stars and >= 100,000 MAU;
  429 returns `retry_after`, honoured with backoff.

`dry_run: true` by default: live delivery requires an explicit flip. A Telegram failure
must never crash the bot (master prompt §19).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from app.core.logging_setup import get_logger
from app.core.timeutils import now_ms

log = get_logger(__name__)


@dataclass
class SendResult:
    ok: bool
    message_id: int | None = None
    status: int = 0
    attempts: int = 0
    error: str = ""
    latency_ms: int = 0
    deadline_missed: bool = False


@dataclass
class TelegramSender:
    cfg: Any
    session: aiohttp.ClientSession | None = None
    _last_send: dict[str, float] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sent: int = 0
    failed: int = 0
    rate_limited: int = 0
    last_error: str = ""
    sent_hashes: set[str] = field(default_factory=set)

    @property
    def dry_run(self) -> bool:
        """True when the sender is configured to log instead of calling Telegram."""
        cfg = getattr(self, "cfg", None)
        tg = getattr(cfg, "telegram", cfg)
        return bool(getattr(tg, "dry_run", True))

    async def __aenter__(self) -> TelegramSender:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15.0))

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    # ------------------------------------------------------------------ sending
    async def _throttle(self, chat_id: str, *, deadline_ms: int | None = None) -> bool:
        async with self._lock:
            min_interval = float(self.cfg.telegram.per_chat_min_interval_sec)
            last = self._last_send.get(chat_id)
            now = time.monotonic()
            if last is not None:
                wait = min_interval - (now - last)
                if wait > 0:
                    if deadline_ms is not None and now_ms() + int(wait * 1000) > deadline_ms:
                        return False
                    await asyncio.sleep(wait)
            if deadline_ms is not None and now_ms() > deadline_ms:
                return False
            self._last_send[chat_id] = time.monotonic()
            return True

    async def send(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        dedupe_key: str | None = None,
        disable_notification: bool = False,
        deadline_ms: int | None = None,
    ) -> SendResult:
        started = now_ms()
        cfg = self.cfg.telegram
        chat_id = chat_id or cfg.chat_id
        if deadline_ms is not None and started > deadline_ms:
            return SendResult(ok=False, attempts=0, error="deadline exceeded before send",
                              latency_ms=0, deadline_missed=True)
        if dedupe_key and dedupe_key in self.sent_hashes:
            return SendResult(ok=True, error="suppressed duplicate", latency_ms=0)

        ceiling = int(cfg.max_chars)
        if len(text) > ceiling:
            text = text[: ceiling - 20] + "\n… [truncated]"

        if cfg.dry_run or not cfg.configured:
            if deadline_ms is not None and now_ms() > deadline_ms:
                self.failed += 1
                return SendResult(ok=False, attempts=0, error="deadline exceeded",
                                  latency_ms=now_ms() - started, deadline_missed=True)
            self.sent += 1
            if dedupe_key:
                self.sent_hashes.add(dedupe_key)
            log.info(
                "telegram dry-run (%s chars, chat=%s)", len(text), "unset" if not chat_id else "set"
            )
            return SendResult(
                ok=True,
                message_id=0,
                status=0,
                attempts=0,
                error="dry_run",
                latency_ms=now_ms() - started,
            )

        url = f"{cfg.api_base}/bot{cfg.bot_token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }
        attempts = 0
        backoff: Sequence[float] = tuple(cfg.retry_backoff_sec)
        while attempts < int(cfg.max_retries):
            attempts += 1
            if not await self._throttle(str(chat_id), deadline_ms=deadline_ms):
                self.failed += 1
                return SendResult(ok=False, attempts=attempts, error="deadline exceeded before throttle",
                                  latency_ms=now_ms() - started, deadline_missed=True)
            try:
                assert self.session is not None
                async with self.session.post(url, json=payload) as resp:
                    status = resp.status
                    body = await resp.json(content_type=None)
                    if status == 200 and body.get("ok"):
                        self.sent += 1
                        if dedupe_key:
                            self.sent_hashes.add(dedupe_key)
                        return SendResult(
                            ok=True,
                            message_id=body.get("result", {}).get("message_id"),
                            status=200,
                            attempts=attempts,
                            latency_ms=now_ms() - started,
                        )
                    if status == 429:
                        self.rate_limited += 1
                        retry_after = float(body.get("parameters", {}).get("retry_after", 0) or 0)
                        if retry_after <= 0:
                            retry_after = backoff[min(attempts - 1, len(backoff) - 1)]
                        log.warning("telegram 429; retrying after %.1f s", retry_after)
                        if deadline_ms is not None and now_ms() + int(retry_after * 1000) > deadline_ms:
                            self.failed += 1
                            return SendResult(ok=False, attempts=attempts, status=status,
                                              error="deadline exceeded after Telegram 429",
                                              latency_ms=now_ms() - started, deadline_missed=True)
                        await asyncio.sleep(retry_after)
                        continue
                    self.failed += 1
                    self.last_error = f"status {status}: {str(body)[:160]}"
                    return SendResult(
                        ok=False,
                        status=status,
                        attempts=attempts,
                        error=self.last_error,
                        latency_ms=now_ms() - started,
                    )
            except Exception as exc:
                self.last_error = str(exc)
                if attempts >= int(cfg.max_retries):
                    break
                delay = backoff[min(attempts - 1, len(backoff) - 1)]
                if deadline_ms is not None and now_ms() + int(delay * 1000) > deadline_ms:
                    self.failed += 1
                    return SendResult(ok=False, attempts=attempts, error="deadline exceeded during retry backoff",
                                      latency_ms=now_ms() - started, deadline_missed=True)
                await asyncio.sleep(delay)
        self.failed += 1
        return SendResult(
            ok=False,
            attempts=attempts,
            error=self.last_error or "send failed",
            latency_ms=now_ms() - started,
        )

    async def health(self) -> dict[str, Any]:
        if self.cfg.telegram.dry_run or not self.cfg.telegram.configured:
            return {
                "state": "DRY_RUN",
                "sent": self.sent,
                "failed": self.failed,
                "configured": self.cfg.telegram.configured,
            }
        try:
            assert self.session is not None
            url = f"{self.cfg.telegram.api_base}/bot{self.cfg.telegram.bot_token}/getMe"
            async with self.session.get(url) as resp:
                body = await resp.json(content_type=None)
                return {
                    "state": "HEALTHY" if body.get("ok") else "UNHEALTHY",
                    "sent": self.sent,
                    "failed": self.failed,
                }
        except Exception as exc:
            return {
                "state": "UNHEALTHY",
                "error": str(exc),
                "sent": self.sent,
                "failed": self.failed,
            }

    def snapshot(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "failed": self.failed,
            "rate_limited": self.rate_limited,
            "dry_run": bool(self.cfg.telegram.dry_run),
            "last_error": self.last_error,
        }


__all__ = ["SendResult", "TelegramSender"]
