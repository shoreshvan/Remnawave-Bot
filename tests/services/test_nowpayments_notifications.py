"""Only a provider-reported underpayment should notify support and the user."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.crud import nowpayments as crud
from app.services.payment.nowpayments import NowpaymentsPaymentMixin
from app.services.admin_notification_service import AdminNotificationService
from app.services import payment_service


@pytest.mark.asyncio
@pytest.mark.parametrize('status,expected', [
    ('waiting', 'pending'), ('confirming', 'processing'),
    ('confirmed', 'processing'), ('sending', 'processing'),
    ('failed', 'failed'), ('expired', 'expired'), ('refunded', 'refunded'),
    ('partially_paid', 'manual_review'),
])
async def test_only_underpayment_notifies(monkeypatch, status, expected):
    payment = SimpleNamespace(id=1, order_id='np1_test', user_id=7,
        status='pending', is_paid=False, processed_events=[],
        nowpayments_payment_id='123', amount_kopeks=5000000)
    monkeypatch.setattr(crud, 'get_nowpayments_payment_by_order_id', AsyncMock(return_value=payment))
    monkeypatch.setattr(crud, 'get_nowpayments_payment_by_id_for_update', AsyncMock(return_value=payment))
    update = AsyncMock()
    monkeypatch.setattr(crud, 'update_nowpayments_payment_status', update)
    admin = AsyncMock()
    monkeypatch.setattr(AdminNotificationService, 'send_admin_notification', admin)
    monkeypatch.setattr(payment_service, 'get_user_by_id', AsyncMock(return_value=
        SimpleNamespace(telegram_id=77, language='fa')))
    service = NowpaymentsPaymentMixin()
    service.bot = SimpleNamespace(send_message=AsyncMock())
    db = SimpleNamespace(commit=AsyncMock())
    payload = dict(order_id=payment.order_id, payment_id='123', payment_status=status,
        actually_paid=0.1 if status == 'partially_paid' else 0, pay_currency='ton')
    assert await service.process_nowpayments_callback(db, payload)
    assert update.await_args.kwargs['status'] == expected
    count = int(status == 'partially_paid')
    assert admin.await_count == count
    assert service.bot.send_message.await_count == count
    # Redelivery must not send the alert a second time.
    assert await service.process_nowpayments_callback(db, payload)
    assert admin.await_count == count
    assert service.bot.send_message.await_count == count
