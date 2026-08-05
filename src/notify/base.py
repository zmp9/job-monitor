"""Pluggable notification layer.

Every channel implements the same contract:

    send(subject: str, body: str) -> bool

`body` is plain text with light markdown. Channels that need HTML wrap it
themselves. Returning False means "did not send" and must not raise — one dead
channel should never take down a run.
"""
from abc import ABC, abstractmethod


class Channel(ABC):
    name = "base"

    @abstractmethod
    def enabled(self) -> bool:
        """True when this channel has the config/secrets it needs."""

    @abstractmethod
    def send(self, subject: str, body: str) -> bool:
        ...


class DryRunChannel(Channel):
    """--dry-run target: prints instead of sending."""
    name = "dry-run"

    def enabled(self) -> bool:
        return True

    def send(self, subject: str, body: str) -> bool:
        print("\n" + "=" * 78)
        print(f"[DRY RUN] would send: {subject}")
        print("=" * 78)
        print(body)
        return True


def dispatch(channels: list[Channel], subject: str, body: str) -> dict:
    """Fan out to every enabled channel. One failure never blocks the others."""
    results = {}
    for ch in channels:
        if not ch.enabled():
            results[ch.name] = "disabled"
            continue
        try:
            results[ch.name] = "sent" if ch.send(subject, body) else "failed"
        except Exception as e:
            results[ch.name] = f"error: {type(e).__name__}: {e}"
    return results
