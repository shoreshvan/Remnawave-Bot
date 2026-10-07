"""NOWPayments client: HMAC-SHA512 webhook signature (docs recipe), invoice body."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest

from app.services.nowpayments_service import NowPaymentsService


def test_verify_callback_signature_docs_recipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact docs recipe: sorted compact JSON + HMAC-SHA512 + IPN secret."""
    from app.config import settings

    monkeypatch.setattr(settings, 'NOWPAYMENTS_IPN_SECRET', 'ipn_secret', raising=False)
    svc = NowPaymentsService()

    payload = {
        'payment_id': '5745459419',
        'payment_status': 'finished',
        'price_amount': 10,
        'price_currency': 'usd',
        'pay_currency': 'trx',
        'order_id': 'np1_x',
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    good = hmac.new(b'ipn_secret', canonical.encode('utf-8'), hashlib.sha512).hexdigest()

    assert svc.build_signature_payload(payload) == canonical
    assert svc.verify_callback_signature(canonical.encode('utf-8'), payload, good) is True
    assert svc.verify_callback_signature(canonical.encode('utf-8'), payload, good.upper()) is True
    assert svc.verify_callback_signature(canonical.encode('utf-8'), payload, 'deadbeef') is False
    assert svc.verify_callback_signature(canonical.encode('utf-8'), payload, None) is False


def test_verify_rejects_when_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, 'NOWPAYMENTS_IPN_SECRET', None, raising=False)
    svc = NowPaymentsService()
    assert svc.verify_callback_signature(b'{}', {}, 'anything') is False


@pytest.mark.asyncio
async def test_create_invoice_prices_usd_not_toman(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = NowPaymentsService()
    captured: dict = {}

    async def fake_request(method, path, *, json_payload=None, allow_404=False):
        captured['method'] = method
        captured['path'] = path
        captured['payload'] = json_payload
        return {
            'id': '4522625843',
            'order_id': 'np1_x',
            'price_amount': 19.61,
            'price_currency': 'usd',
            'invoice_url': 'https://nowpayments.io/payment/?iid=4522625843',
        }

    monkeypatch.setattr(svc, '_request', AsyncMock(side_effect=fake_request))

    data = await svc.create_invoice(order_id='np1_x', price_usd=19.61)

    assert captured['method'] == 'POST'
    assert captured['path'] == '/invoice'
    # Provider knows no IRR — price goes in USD, never Toman/kopeks.
    assert captured['payload']['price_amount'] == 19.61
    assert captured['payload']['price_currency'] == 'usd'
    assert 'pay_currency' not in captured['payload']  # buyer picks on the hosted page
    assert data['invoice_url'].startswith('https://')


def test_toman_to_usd_rate() -> None:
    from app.config import settings

    assert settings.nowpayments_toman_to_usd(2550000000) == pytest.approx(100.0)
    assert settings.nowpayments_toman_to_usd(500000000) == pytest.approx(19.61, abs=0.01)
