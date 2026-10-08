"""WhatsApp alerts to the firm (Meta WhatsApp Cloud API).

Sent when a slot is booked, when a visitor says they have made a transfer, when
a payment lands, and when a late payment finds its slot gone.

WhatsApp only lets a business start a conversation with an approved message
template. Set WHATSAPP_TEMPLATE to a template whose body has one variable, e.g.

    New consultation booking update: {{1}}

and every alert is sent through it. Without a template the alert goes as plain
text, which WhatsApp delivers only within 24 hours of the recipient last
messaging the business number. That is enough for a demo: send "hi" to the
test number from your phone first.

No WHATSAPP_TOKEN: alerts are printed to the log instead. Sending never raises,
because a booking must not fail over a notification.
"""

import os
import sys

import requests

GRAPH = "https://graph.facebook.com/v21.0"


class WhatsAppAlerts:
    def __init__(self, token: str, phone_number_id: str, recipients: list[str],
                 template: str | None, language: str):
        self.token = token
        self.url = f"{GRAPH}/{phone_number_id}/messages"
        self.recipients = recipients
        self.template = template
        self.language = language

    def _payload(self, to: str, text: str) -> dict:
        if self.template:
            # Template variables may not contain newlines, tabs or 4+ spaces in a row.
            flat = " ".join(text.split())[:1000]
            return {"messaging_product": "whatsapp", "to": to, "type": "template",
                    "template": {"name": self.template, "language": {"code": self.language},
                                 "components": [{"type": "body", "parameters": [
                                     {"type": "text", "text": flat}]}]}}
        return {"messaging_product": "whatsapp", "to": to, "type": "text",
                "text": {"body": text, "preview_url": False}}

    def send(self, text: str) -> None:
        for to in self.recipients:
            try:
                resp = requests.post(self.url, timeout=15, json=self._payload(to, text),
                                     headers={"Authorization": f"Bearer {self.token}"})
                if resp.status_code >= 400:
                    print(f"[WARN] WhatsApp alert to ...{to[-4:]} failed: "
                          f"{resp.status_code} {resp.text[:300]}", file=sys.stderr)
            except requests.RequestException as exc:
                print(f"[WARN] WhatsApp alert to ...{to[-4:]} failed: {exc}", file=sys.stderr)


class LogAlerts:
    """Development stand-in: print the alert instead of sending it."""

    def send(self, text: str) -> None:
        print(f"[alert] {text}", file=sys.stderr)


def make_alerts(env=os.environ):
    token = env.get("WHATSAPP_TOKEN", "").strip()
    recipients = ["".join(c for c in n if c.isdigit())
                  for n in env.get("FIRM_ALERT_NUMBERS", "").split(",") if n.strip()]
    if not (token and env.get("WHATSAPP_PHONE_NUMBER_ID") and recipients):
        print("[startup] alerts: log only (set WHATSAPP_* and FIRM_ALERT_NUMBERS)", file=sys.stderr)
        return LogAlerts()
    print(f"[startup] alerts: WhatsApp to {len(recipients)} number(s)", file=sys.stderr)
    return WhatsAppAlerts(token, env["WHATSAPP_PHONE_NUMBER_ID"], recipients,
                          env.get("WHATSAPP_TEMPLATE") or None,
                          env.get("WHATSAPP_TEMPLATE_LANG", "en"))
