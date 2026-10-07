"""AtlasPay client: amount conversion, HMAC webhook signature, order body."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest

from app.services.atlaspay_service import AtlasPayService, kopeks_to_toman, toman_to_kopeks


def test_toman_kopeks_roundtrip() -> None:
    # Provider speaks whole Toman; we store 1/100-Toman kopeks.
    assert toman_to_kopeks(50000) == 5000000
    assert toman_to_kopeks('50000') == 5000000
    assert kopeks_to_toman(5000000) == 50000


def test_toman_to_kopeks_rejects_junk() -> None:
    assert toman_to_kopeks(None) is None
    assert toman_to_kopeks(True) is None
    assert toman_to_kopeks('abc') is None
    assert toman_to_kopeks('12.5') is None  # fractional Toman is not accepted


def test_verify_callback_signature_raw_body(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, 'ATLASPAY_WEBHOOK_SECRET', 'wh_secret', raising=False)
    svc = AtlasPayService()

    payload = {'event': 'order.confirmed', 'orderId': 66, 'merchantOrderRef': 'ap1_x', 'status': 'confirmed'}
    raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    good = hmac.new(b'wh_secret', raw, hashlib.sha256).hexdigest()

    assert svc.verify_callback_signature(raw, payload, good) is True
    assert svc.verify_callback_signature(raw, payload, good.upper()) is True  # case-insensitive hex
    assert svc.verify_callback_signature(raw, payload, 'deadbeef') is False
    assert svc.verify_callback_signature(raw, payload, None) is False


def test_verify_callback_signature_reformatted_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy may reformat spacing — canonical re-serialization must still verify."""
    from app.config import settings

    monkeypatch.setattr(settings, 'ATLASPAY_WEBHOOK_SECRET', 'wh_secret', raising=False)
    svc = AtlasPayService()

    payload = {'event': 'order.confirmed', 'orderId': 66, 'status': 'confirmed'}
    canonical = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    good = hmac.new(b'wh_secret', canonical, hashlib.sha256).hexdigest()

    reformatted = json.dumps(payload, indent=2).encode('utf-8')
    assert svc.verify_callback_signature(reformatted, payload, good) is True


def test_verify_rejects_when_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, 'ATLASPAY_WEBHOOK_SECRET', None, raising=False)
    svc = AtlasPayService()
    assert svc.verify_callback_signature(b'{}', {}, 'anything') is False


@pytest.mark.asyncio
async def test_create_order_sends_whole_toman(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = AtlasPayService()
    captured: dict = {}

    async def fake_request(method, path, *, json_payload=None, allow_404=False):
        captured['method'] = method
        captured['path'] = path
        captured['payload'] = json_payload
        return {
            'orderId': 66,
            'trackingCode': '5c23c12c9fa0c8b3',
            'totalAmountToman': 250017,
            'paymentDeadlineAt': '2026-08-05T21:58:56.329Z',
            'customerStartLink': 'https://t.me/atlaspaybot/pay?startapp=order_66_x',
        }

    monkeypatch.setattr(svc, '_request', AsyncMock(side_effect=fake_request))

    data = await svc.create_order(
        merchant_order_ref='ap123_abcdef12', amount_kopeks=25000000, customer_telegram_id=123
    )

    assert captured['method'] == 'POST'
    assert captured['path'] == '/orders'
    # 25000000 kopeks -> 250000 whole Toman
    assert captured['payload']['baseAmountToman'] == 250000
    assert captured['payload']['merchantOrderRef'] == 'ap123_abcdef12'
    assert captured['payload']['customerTelegramId'] == 123
    assert data['trackingCode'] == '5c23c12c9fa0c8b3'
    assert data['totalAmountToman'] == 250017


@pytest.mark.asyncio
async def test_create_order_omits_optional_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = AtlasPayService()
    captured: dict = {}

    async def fake_request(method, path, *, json_payload=None, allow_404=False):
        captured['payload'] = json_payload
        return {'orderId': 1, 'customerStartLink': 'https://t.me/x'}

    monkeypatch.setattr(svc, '_request', AsyncMock(side_effect=fake_request))

    # Guest flow: no Telegram id, no per-order webhook url.
    await svc.create_order(merchant_order_ref='apguest_x', amount_kopeks=5000000)
    assert 'customerTelegramId' not in captured['payload']
    assert 'webhookUrl' not in captured['payload']
