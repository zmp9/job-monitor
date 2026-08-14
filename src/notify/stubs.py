"""Non-email notification channels.

Each is wired into dispatch() and reports "disabled" until its secrets exist, so
turning one on is a matter of adding secrets — no restructuring.

PushChannel (ntfy) is implemented. SMSChannel is still a stub: US carriers now
require A2P 10DLC brand+campaign registration before Twilio will deliver to a
mobile number, which costs money and takes days to approve.
"""
import os
import re
import urllib.error
import urllib.request

from .base import Channel

UA = "job-monitor/1.0 (+https://github.com/zmp9/job-monitor)"


def _env(name: str, default: str = "") -> str:
    """Strip surrounding whitespace — secrets pasted into the GitHub UI routinely
    carry a trailing newline, which is illegal in an HTTP header value."""
    return (os.environ.get(name, default) or "").strip()


def _ascii_header(s: str, limit: int = 120) -> str:
    """ntfy rejects raw UTF-8 in the Title header, and our subjects contain an
    em-dash. Fold to ASCII rather than letting the send fail."""
    s = s.replace("—", "-").replace("–", "-").replace("’", "'")
    return re.sub(r"[^\x20-\x7e]", "", s)[:limit]


class SMSChannel(Channel):
    """Twilio SMS.

    TODO to enable:
      1. Twilio account -> get Account SID, Auth Token, and a sending number.
      2. Repo secrets: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, SMS_TO
      3. POST https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json
         basic-auth (SID, token), form body: From, To, Body
      4. SMS is 1600 chars max — send subject + top 3 matches + a link only,
         not the full body. Add a truncate step here.
    """
    name = "sms"

    def enabled(self) -> bool:
        return all(_env(k) for k in
                   ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "SMS_TO"))

    def send(self, subject: str, body: str, html: str = None) -> bool:
        raise NotImplementedError("Twilio SMS not wired yet — see TODO in stubs.py")


class PushChannel(Channel):
    """Phone push via ntfy.sh.

    No account needed: POST to https://ntfy.sh/<topic> and subscribe to that
    topic in the ntfy app. Set NTFY_TOPIC.

    SECURITY: ntfy topics are unauthenticated — anyone who guesses the name
    reads your alerts. Use a long random topic and keep it in repo secrets.

    Pushover remains an option (PUSHOVER_TOKEN + PUSHOVER_USER); not wired,
    since ntfy covers the same need without a paid licence.
    """
    name = "push"
    MAX_MATCHES = 3

    def enabled(self) -> bool:
        return bool(_env("NTFY_TOPIC"))

    def summarize(self, subject: str, body: str) -> str:
        """Push is a glance surface, not a document.

        The email body runs to hundreds of lines; phones truncate long pushes
        anyway. Pull out the top few "[score] Title" lines plus the company
        line that follows each, and point at the email for the rest.
        """
        lines = body.split("\n")
        picked, i = [], 0
        while i < len(lines) and len(picked) < self.MAX_MATCHES:
            m = re.match(r"^\[(\d+)\]\s+(.*)$", lines[i])
            if m:
                score, title = m.group(1), m.group(2).strip()
                company = ""
                if i + 1 < len(lines):
                    company = lines[i + 1].strip().split("—")[0].strip()
                picked.append(f"{score} · {title[:60]}" + (f" · {company[:28]}" if company else ""))
            i += 1
        if not picked:
            return body[:300]
        total = len(re.findall(r"(?m)^\[\d+\]\s", body))
        out = "\n".join(picked)
        if total > len(picked):
            out += f"\n+{total - len(picked)} more — see email"
        return out

    def send(self, subject: str, body: str, html: str = None) -> bool:
        topic = _env("NTFY_TOPIC")
        payload = self.summarize(subject, body).encode("utf-8")
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=payload, method="POST",
            headers={"Title": _ascii_header(subject),
                     "Priority": "default",
                     "Tags": "briefcase",
                     "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as f:
                return 200 <= f.status < 300
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise RuntimeError(f"ntfy rejected push: HTTP {e.code} {detail}") from None
