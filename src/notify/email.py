"""Email channel.

Two backends, selected by EMAIL_BACKEND (default "resend").

Recommendation: Resend. Gmail SMTP needs an app password, silently rate-limits
personal accounts, and Google periodically revokes app passwords — which fails
at 6am inside a cron run you aren't watching. Resend is one API key, one HTTPS
call, 3k emails/month free, and it fits the send(subject, body) seam without
smtplib's connection handling.

SMTP is kept as a fallback so you aren't blocked on signing up for anything.

Secrets (GitHub repo secrets, never committed):
    RESEND_API_KEY, EMAIL_FROM, EMAIL_TO          # resend backend
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO
"""
import json
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

from .base import Channel


def _env(name: str, default: str = "") -> str:
    """Read an env var with surrounding whitespace stripped.

    Secrets pasted into the GitHub UI routinely carry a trailing newline. That
    newline is illegal in an HTTP header value, so an otherwise-valid API key
    failed with "Invalid header value" rather than anything mentioning the key.
    """
    return (os.environ.get(name, default) or "").strip()


def _html(body: str) -> str:
    esc = (body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return f'<pre style="font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;white-space:pre-wrap">{esc}</pre>'


class EmailChannel(Channel):
    name = "email"

    def __init__(self):
        self.backend = _env("EMAIL_BACKEND", "resend").lower()
        self.to = _env("EMAIL_TO")
        self.sender = _env("EMAIL_FROM")

    def enabled(self) -> bool:
        if not (self.to and self.sender):
            return False
        if self.backend == "resend":
            return bool(_env("RESEND_API_KEY"))
        return bool(_env("SMTP_HOST") and _env("SMTP_PASSWORD"))

    def send(self, subject: str, body: str, html: str = None) -> bool:
        # Fall back to the <pre> wrapper only when no rich rendering was supplied.
        html = html or _html(body)
        return (self._resend(subject, body, html) if self.backend == "resend"
                else self._smtp(subject, body, html))

    def _resend(self, subject: str, body: str, html: str) -> bool:
        payload = json.dumps({
            "from": self.sender,
            "to": [a.strip() for a in self.to.split(",") if a.strip()],
            "subject": subject,
            "html": html,
            "text": body,
        }).encode()
        # A User-Agent is required: Resend's API is behind Cloudflare, which
        # rejects urllib's default "Python-urllib/3.x" signature with HTTP 403
        # "error code: 1010" before the request ever reaches Resend — so the API
        # key is never even evaluated.
        req = urllib.request.Request(
            "https://api.resend.com/emails", data=payload, method="POST",
            headers={"Authorization": f"Bearer {_env('RESEND_API_KEY')}",
                     "Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": "job-monitor/1.0 (+https://github.com/zmp9/job-monitor)"})
        try:
            with urllib.request.urlopen(req, timeout=30) as f:
                return 200 <= f.status < 300
        except urllib.error.HTTPError as e:
            # Resend explains rejections in the JSON body ("domain not verified",
            # "you can only send to your own address", bad key). A bare
            # "HTTP Error 403" hides all of that, so surface it.
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise RuntimeError(f"Resend rejected send: HTTP {e.code} {detail}") from None

    def _smtp(self, subject: str, body: str, html: str) -> bool:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = self.to
        msg.set_content(body)
        msg.add_alternative(html, subtype="html")
        host = _env("SMTP_HOST")
        port = int(_env("SMTP_PORT", "587") or 587)
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(_env("SMTP_USER") or self.sender, _env("SMTP_PASSWORD"))
            s.send_message(msg)
        return True
