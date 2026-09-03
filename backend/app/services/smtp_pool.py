from __future__ import annotations

import smtplib
import threading
import time
from collections import deque
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable

from app.config import settings


@dataclass
class _ConnectionSlot:
    client: smtplib.SMTP | smtplib.SMTP_SSL | None = None
    connected_at: float = 0.0
    last_used_at: float = 0.0
    message_count: int = 0

    def close(self) -> None:
        client, self.client = self.client, None
        self.connected_at = 0.0
        self.last_used_at = 0.0
        self.message_count = 0
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass


class SmtpConnectionPool:
    """A bounded pool where each checked-out SMTP client is used serially."""

    def __init__(self) -> None:
        size = max(1, int(settings.SMTP_MAX_CONNECTIONS))
        self._slots = [_ConnectionSlot() for _ in range(size)]
        self._available = deque(range(size))
        self._condition = threading.Condition()
        self._rate_lock = threading.Lock()
        self._send_timestamps: deque[float] = deque()

    def _connect(self) -> smtplib.SMTP | smtplib.SMTP_SSL:
        timeout = max(1, int(settings.SMTP_CONNECT_TIMEOUT_SECONDS))
        if settings.SMTP_PORT == 465:
            client: smtplib.SMTP | smtplib.SMTP_SSL = smtplib.SMTP_SSL(
                settings.SMTP_HOST,
                settings.SMTP_PORT,
                timeout=timeout,
            )
        else:
            client = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout)
            client.starttls()
        client.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        if getattr(client, "sock", None) is not None:
            client.sock.settimeout(max(1, int(settings.SMTP_SEND_TIMEOUT_SECONDS)))
        return client

    def _requires_reconnect(self, slot: _ConnectionSlot, now: float) -> bool:
        if slot.client is None:
            return True
        if slot.message_count >= max(1, int(settings.SMTP_MESSAGES_PER_CONNECTION)):
            return True
        if now - slot.connected_at >= max(1, int(settings.SMTP_CONNECTION_MAX_AGE_SECONDS)):
            return True
        if slot.last_used_at and now - slot.last_used_at >= max(1, int(settings.SMTP_IDLE_TIMEOUT_SECONDS)):
            return True
        try:
            code, _ = slot.client.noop()
            return int(code) != 250
        except Exception:
            return True

    def _rate_limit(self) -> None:
        limit = max(1, int(settings.SMTP_RATE_LIMIT_PER_MINUTE))
        while True:
            with self._rate_lock:
                now = time.monotonic()
                while self._send_timestamps and now - self._send_timestamps[0] >= 60:
                    self._send_timestamps.popleft()
                if len(self._send_timestamps) < limit:
                    self._send_timestamps.append(now)
                    return
                wait_for = max(0.01, 60 - (now - self._send_timestamps[0]))
            time.sleep(min(wait_for, 1.0))

    def _send(self, sender: Callable[[smtplib.SMTP | smtplib.SMTP_SSL], dict[str, tuple[int, bytes]]]) -> dict[str, tuple[int, bytes]]:
        with self._condition:
            while not self._available:
                self._condition.wait()
            index = self._available.popleft()
        slot = self._slots[index]
        try:
            self._rate_limit()
            now = time.monotonic()
            if self._requires_reconnect(slot, now):
                slot.close()
                slot.client = self._connect()
                slot.connected_at = now
            assert slot.client is not None
            refused = sender(slot.client)
            slot.message_count += 1
            slot.last_used_at = time.monotonic()
            return refused
        except Exception:
            slot.close()
            raise
        finally:
            with self._condition:
                self._available.append(index)
                self._condition.notify()

    def send_message(self, message: EmailMessage) -> dict[str, tuple[int, bytes]]:
        return self._send(lambda client: client.send_message(message))

    def send_raw(
        self,
        *,
        from_address: str,
        recipients: list[str],
        raw_message: bytes,
    ) -> dict[str, tuple[int, bytes]]:
        return self._send(
            lambda client: client.sendmail(from_address, recipients, raw_message)
        )

    def close(self) -> None:
        for slot in self._slots:
            slot.close()


_pool: SmtpConnectionPool | None = None
_pool_signature: tuple[str, int, str, int] | None = None
_pool_lock = threading.Lock()


def smtp_connection_pool() -> SmtpConnectionPool:
    global _pool, _pool_signature
    signature = (
        settings.SMTP_HOST,
        int(settings.SMTP_PORT),
        settings.SMTP_USER,
        max(1, int(settings.SMTP_MAX_CONNECTIONS)),
    )
    with _pool_lock:
        if _pool is None or _pool_signature != signature:
            if _pool is not None:
                _pool.close()
            _pool = SmtpConnectionPool()
            _pool_signature = signature
        return _pool


def reset_smtp_connection_pool() -> None:
    global _pool, _pool_signature
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _pool_signature = None
