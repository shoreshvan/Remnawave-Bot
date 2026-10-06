"""TonPays client: amount conversion, webhook auth, invoice body."""

from unittest.mock import AsyncMock

import pytest

from app.services.tonpays_service import TonPaysService, kopeks_to_toman, toman_to_kopeks


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


def test_verify_callback_auth_accepts_matching_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, 'TONPAYS_API_KEY', 'tg_key_123', raising=False)
    svc = TonPaysService()

    payload = {
        'invoice_id': 'TP-CG123XYZ0',
        'order_id': 'tpabc123',
        'status': 'completed',
        'paid': True,
        'delivery_id': 'TP-CG123XYZ0:completed:1727200000',
    }
    assert svc.verify_callback_auth({'X-API-Key': 'tg_key_123'}, payload) is True
    # Wrong key, missing key, missing identity — all rejected.
    assert svc.verify_callback_auth({'X-API-Key': 'other'}, payload) is False
    assert svc.verify_callback_auth({}, payload) is False
    assert svc.verify_callback_auth({'X-API-Key': 'tg_key_123'}, {}) is False


def test_verify_rejects_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, 'TONPAYS_API_KEY', None, raising=False)
    svc = TonPaysService()
    assert svc.verify_callback_auth({'X-API-Key': 'anything'}, {'delivery_id': 'x'}) is False


@pytest.mark.asyncio
async def test_create_invoice_sends_whole_toman_and_short_order_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = TonPaysService()
    captured: dict = {}

    async def fake_request(method, path, *, json_payload=None, allow_404=False):
        captured['method'] = method
        captured['path'] = path
        captured['payload'] = json_payload
        return {
            'invoice_id': 'TP-CG123XYZ0',
            'order_id': 'tpabc123',
            'request_amount': 50000,
            'final_amount': 50037,
            'status': 'pending',
            'card_number': '6037········1234',
            'card_name': 'holder',
        }

    monkeypatch.setattr(svc, '_request', AsyncMock(side_effect=fake_request))

    data = await svc.create_invoice(order_id='tpabc123', amount_kopeks=5000000, buyer_chat_id=123456789)

    assert captured['method'] == 'POST'
    assert captured['path'] == '/api/custom/v1/invoices/telegram/create'
    # 5000000 kopeks -> 50000 whole Toman
    assert captured['payload']['amount'] == 50000
    assert captured['payload']['buyer_chat_id'] == 123456789
    # Provider caps order_id at 20 chars.
    assert len(captured['payload']['order_id']) <= 20
    assert data['invoice_id'] == 'TP-CG123XYZ0'
    assert data['final_amount'] == 50037
