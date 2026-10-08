"""Paystack for the consultation fee: card, bank transfer and USSD in one checkout.

Paystack's checkout has a "Pay with Transfer" option that issues a one-off
account number and confirms the payment automatically, so a visitor who prefers
transfer still gets an instant confirmation. Direct transfer to the firm's own
account is handled in app.py instead, because the firm has to check its bank.

A payment counts only when Paystack says so from its side: a signed webhook
(charge.success) or a server-to-server verify call. The browser returning from
checkout proves nothing.

PAYSTACK_SECRET_KEY unset = online payment is switched off and the booking page
offers direct transfer only. Use an sk_test_ key for the demo.
"""

import hashlib
import hmac
import os
import secrets

import requests

API = "https://api.paystack.co"


class PaymentError(Exception):
    pass


class Paystack:
    def __init__(self, secret_key: str):
        self.secret = secret_key
        self.headers = {"Authorization": f"Bearer {secret_key}"}

    @property
    def test_mode(self) -> bool:
        return self.secret.startswith("sk_test_")

    def start(self, *, email: str, amount_kobo: int, booking_ref: str, callback_url: str,
              metadata: dict) -> tuple[str, str]:
        """Open a checkout. Returns (payment reference, URL to send the visitor to).

        Each attempt gets its own reference: Paystack refuses to reuse one, and a
        visitor may close the checkout and try again.
        """
        pay_ref = f"{booking_ref}-{secrets.token_hex(4)}"
        resp = requests.post(f"{API}/transaction/initialize", headers=self.headers, timeout=15, json={
            "email": email, "amount": amount_kobo, "currency": "NGN", "reference": pay_ref,
            "callback_url": callback_url, "metadata": {**metadata, "booking_ref": booking_ref},
        })
        body = resp.json() if resp.content else {}
        if resp.status_code >= 400 or not body.get("status"):
            raise PaymentError(f"initialize: {resp.status_code} {body.get('message')}")
        return pay_ref, body["data"]["authorization_url"]

    def verify(self, pay_ref: str) -> dict:
        """Paystack's record of one payment attempt (status, amount, currency)."""
        resp = requests.get(f"{API}/transaction/verify/{pay_ref}", headers=self.headers, timeout=15)
        body = resp.json() if resp.content else {}
        if resp.status_code >= 400 or not body.get("status"):
            raise PaymentError(f"verify: {resp.status_code} {body.get('message')}")
        return body["data"]

    def signed(self, raw_body: bytes, signature: str | None) -> bool:
        """Is this webhook really from Paystack? (HMAC-SHA512 of the raw body.)"""
        expected = hmac.new(self.secret.encode(), raw_body, hashlib.sha512).hexdigest()
        return bool(signature) and hmac.compare_digest(expected, signature)


def settled(data: dict, amount_kobo: int) -> bool:
    """A successful NGN payment of at least the fee."""
    return (data.get("status") == "success" and data.get("currency") == "NGN"
            and int(data.get("amount") or 0) >= amount_kobo)


def make_paystack(env=os.environ) -> Paystack | None:
    key = env.get("PAYSTACK_SECRET_KEY", "").strip()
    return Paystack(key) if key else None
