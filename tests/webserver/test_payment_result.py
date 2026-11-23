from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.services.payment.nowpayments import NowpaymentsPaymentMixin
from app.utils.payment_result_token import payment_result_token
from app.webserver import payment_result as result


@pytest.fixture
def payment():
    return SimpleNamespace(
        order_id='np-test', invoice_id='12345', amount_kopeks=5000000,
        is_paid=False, transaction_id=None, status='pending', metadata_json={},
        created_at=datetime.now(UTC), paid_at=None, pay_currency=None,
        actually_paid=None, processed_events=[],
    )


@pytest.mark.asyncio
async def test_only_signed_link_can_read_payment(monkeypatch, payment):
    app = FastAPI()
    app.include_router(result.create_payment_result_router())
    app.dependency_overrides[result.get_db] = lambda: object()
    lookup = AsyncMock(return_value=payment)
    monkeypatch.setattr(result, 'get_nowpayments_payment_by_order_id', lookup)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        path = settings.NOWPAYMENTS_RESULT_PATH + '/status'
        invalid = await client.get(path, params={'invoice':'np-test', 'token':'0'*32, 'status':'ok'})
        assert invalid.status_code == 404
        lookup.assert_not_awaited()
        valid = await client.get(path, params={
            'invoice':'np-test', 'token':payment_result_token('np-test'), 'status':'ok', 'amount':'999999',
        })
        assert valid.status_code == 200
        assert valid.json()['status'] == 'pending'
        assert valid.json()['amount_toman'] == '50000'
        assert valid.headers['cache-control'] == 'no-store'
        assert 'user_id' not in valid.json()
    assert app.openapi()


@pytest.mark.parametrize('status', ['pending','processing','manual_review','amount_mismatch','failed','expired','refunded'])
def test_status_does_not_claim_balance_credited(payment, status):
    payment.status = status
    data = result.payment_result_data(payment)
    assert data['status'] == status
    assert data['credited'] is False


def test_success_requires_recorded_transaction(payment):
    payment.status = 'success'
    payment.is_paid = True
    assert result.payment_result_data(payment)['status'] == 'processing'
    payment.transaction_id = 88
    assert result.payment_result_data(payment)['credited'] is True


def test_short_redirect_has_token_and_no_fake_display_data(monkeypatch):
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://example.com')
    url = NowpaymentsPaymentMixin()._build_result_url(status='ok', order_id='np-test', amount_kopeks=5000000)
    query = parse_qs(urlsplit(url).query)
    assert query['token'] == [payment_result_token('np-test')]
    assert 'amount' not in query
    assert len(url) < 240
