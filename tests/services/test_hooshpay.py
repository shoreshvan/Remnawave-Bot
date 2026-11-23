"""HooshPay client: amount conversion, PHP-compatible webhook signature, invoice body."""

import hashlib
import hmac
from unittest.mock import AsyncMock

import pytest

from app.services.hooshpay_service import HooshPayService, kopeks_to_toman, toman_to_kopeks


def test_toman_kopeks_roundtrip() -> None:
    # Provider speaks whole Toman; we store 1/100-Toman kopeks.
    assert toman_to_kopeks(250000) == 25000000
    assert toman_to_kopeks('250000') == 25000000
    assert kopeks_to_toman(25000000) == 250000
    # min 1000 Toman == 100000 kopeks
    assert kopeks_to_toman(100000) == 1000


def test_toman_to_kopeks_rejects_junk() -> None:
    assert toman_to_kopeks(None) is None
    assert toman_to_kopeks(True) is None
    assert toman_to_kopeks('abc') is None
    assert toman_to_kopeks('12.5') is None  # fractional Toman is not accepted


def test_signature_payload_matches_php_json_encode() -> None:
    """build_signature_payload must byte-match PHP ksort + json_encode(UNESCAPED_*)."""
    svc = HooshPayService()
    # keys sorted, no spaces around separators
    assert svc.build_signature_payload({'b': 2, 'a': 1}) == '{"a":1,"b":2}'
    # UNESCAPED_UNICODE: Persian stays as-is, not \uXXXX
    assert svc.build_signature_payload({'name': 'تست'}) == '{"name":"تست"}'
    # UNESCAPED_SLASHES: forward slash not escaped
    assert svc.build_signature_payload({'url': 'a/b'}) == '{"url":"a/b"}'


def test_verify_callback_signature_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, 'HOOSHPAY_API_SECRET', 'sk_secret', raising=False)
    svc = HooshPayService()

    payload = {
        'event': 'payment.success',
        'invoice': 'inv_AbC123xyz',
        'order_id': 'hp42_deadbeef',
        'status': 'paid',
        'amount': 250000,
        'payable_amount': 300017,
        'merchant_credit': 250000,
        'tracking_code': '556677',
    }
    body = svc.build_signature_payload(payload)
    good = hmac.new(b'sk_secret', body.encode('utf-8'), hashlib.sha256).hexdigest()

    assert svc.verify_callback_signature(payload, good) is True
    assert svc.verify_callback_signature(payload, good.upper()) is True  # case-insensitive hex
    assert svc.verify_callback_signature(payload, 'deadbeef') is False
    assert svc.verify_callback_signature(payload, None) is False


def test_verify_rejects_when_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, 'HOOSHPAY_API_SECRET', None, raising=False)
    svc = HooshPayService()
    # An empty key would make the HMAC forgeable — must refuse outright.
    assert svc.verify_callback_signature({'a': 1}, 'anything') is False


@pytest.mark.asyncio
async def test_create_invoice_sends_whole_toman_and_omits_fee_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = HooshPayService()
    captured: dict = {}

    async def fake_request(method, path, *, json_payload=None, allow_404=False):
        captured['method'] = method
        captured['path'] = path
        captured['payload'] = json_payload
        return {'success': True, 'data': {'uid': 'inv_1', 'payment_url': 'https://hooshpay.xyz/pay/inv_1', 'payable_amount': 300017}}

    monkeypatch.setattr(svc, '_request', AsyncMock(side_effect=fake_request))

    data = await svc.create_invoice(order_id='hp42_x', amount_kopeks=25000000, description='top-up')

    assert captured['method'] == 'POST'
    assert captured['path'] == '/invoices'
    # 25000000 kopeks -> 250000 whole Toman
    assert captured['payload']['amount'] == 250000
    assert captured['payload']['order_id'] == 'hp42_x'
    # fee_mode left empty -> not sent, so HooshPay uses the account default
    assert 'fee_mode' not in captured['payload']
    assert data['uid'] == 'inv_1'
    assert data['payment_url'].endswith('/pay/inv_1')


@pytest.mark.asyncio
async def test_create_invoice_includes_fee_mode_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = HooshPayService()
    captured: dict = {}

    async def fake_request(method, path, *, json_payload=None, allow_404=False):
        captured['payload'] = json_payload
        return {'success': True, 'data': {'uid': 'inv_2', 'payment_url': 'https://x/pay/inv_2'}}

    monkeypatch.setattr(svc, '_request', AsyncMock(side_effect=fake_request))
    await svc.create_invoice(order_id='o', amount_kopeks=100000, fee_mode='buyer')
    assert captured['payload']['fee_mode'] == 'buyer'
    assert captured['payload']['amount'] == 1000
