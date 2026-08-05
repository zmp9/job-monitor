"""Seams for the three channels you plan to enable later.

Each is wired into dispatch() and reports "disabled" until its secrets exist, so
turning one on is a matter of adding secrets and finishing the marked TODO — no
restructuring.
"""
import os

from .base import Channel


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
        return all(os.environ.get(k) for k in
                   ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "SMS_TO"))

    def send(self, subject: str, body: str) -> bool:
        raise NotImplementedError("Twilio SMS not wired yet — see TODO in stubs.py")


class PushChannel(Channel):
    """Pushover or ntfy.

    ntfy is the lower-friction option: no account, POST a body to
    https://ntfy.sh/<your-random-topic> and subscribe in the phone app.
    Pushover is more reliable but is a one-off paid license.

    TODO to enable:
      1. Pick one. For ntfy set NTFY_TOPIC; for Pushover set
         PUSHOVER_TOKEN + PUSHOVER_USER.
      2. ntfy:     POST https://ntfy.sh/{topic}, body=text, header Title: subject
         Pushover: POST https://api.pushover.net/1/messages.json
                   form: token, user, title, message
      3. Push is a glance surface — send count + best match only.
    """
    name = "push"

    def enabled(self) -> bool:
        return bool(os.environ.get("NTFY_TOPIC") or
                    (os.environ.get("PUSHOVER_TOKEN") and os.environ.get("PUSHOVER_USER")))

    def send(self, subject: str, body: str) -> bool:
        raise NotImplementedError("Push not wired yet — see TODO in stubs.py")
