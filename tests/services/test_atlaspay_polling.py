import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.crud import atlaspay as crud
from app.services import atlaspay_polling_service as polling
from app.services.payment.atlaspay import AtlaspayPaymentMixin, atlaspay_service


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['awaiting_payment', 'confirmed', 'settled'])
async def test_poll_uses_get_and_verifies_success(monkeypatch, status):
    payment = SimpleNamespace(id=1, order_id='ref', atlaspay_payment_id='42', is_paid=False, status='pending')
    monkeypatch.setattr(crud, 'get_atlaspay_payment_by_order_id', AsyncMock(return_value=payment))
    monkeypatch.setattr(crud, 'get_atlaspay_payment_by_id_for_update', AsyncMock(return_value=payment))
    monkeypatch.setattr(crud, 'is_atlaspay_event_processed', lambda *args: True)
    get = AsyncMock(return_value={'status': status})
    verify = AsyncMock(return_value={'status': status, 'paid': True, 'requiresManualDelivery': False})
    monkeypatch.setattr(atlaspay_service, 'get_order', get)
    monkeypatch.setattr(atlaspay_service, 'verify_order', verify)
    service = AtlaspayPaymentMixin()
    service._apply_atlaspay_status = AsyncMock(return_value=True)
    await service.check_atlaspay_payment_status(AsyncMock(), 'ref', polling=True)
    get.assert_awaited_once_with(order_id='42')
    assert verify.await_count == (status in {'confirmed', 'settled'})
    service._apply_atlaspay_status.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'paid,manual,expected',
    [(False, False, 'processing'), (True, True, 'manual_review'), (True, False, 'credited')],
)
async def test_credit_requires_full_verified_payment(monkeypatch, paid, manual, expected):
    payment = SimpleNamespace(order_id='ref', is_paid=False, amount_kopeks=5000000, total_amount_kopeks=5000100)
    monkeypatch.setattr(crud, 'remember_atlaspay_event', lambda *args: None)
    update = AsyncMock()
    monkeypatch.setattr(crud, 'update_atlaspay_payment_status', update)
    service = AtlaspayPaymentMixin()
    service._apply_atlaspay_success = AsyncMock(return_value=True)
    await service._apply_atlaspay_status(
        AsyncMock(), payment=payment, event_key='same', trigger='api_check',
        status_data={'status': 'confirmed', 'paid': paid, 'requiresManualDelivery': manual,
                     'totalAmountToman': 50001},
    )
    if expected == 'credited':
        service._apply_atlaspay_success.assert_awaited_once()
    else:
        service._apply_atlaspay_success.assert_not_awaited()
        assert update.call_args.kwargs['status'] == expected


@pytest.mark.asyncio
async def test_poll_runs_with_general_checker_disabled_and_stops(monkeypatch):
    monkeypatch.setattr(polling.settings, 'ATLASPAY_ENABLED', True)
    monkeypatch.setattr(polling.settings, 'ATLASPAY_API_KEY', 'test')
    monkeypatch.setattr(polling.settings, 'PAYMENT_VERIFICATION_AUTO_CHECK_ENABLED', False)
    service = polling.AtlasPayPollingService()
    checked = asyncio.Event()
    service.check_pending = AsyncMock(side_effect=lambda _: checked.set())
    await service.start(object())
    try:
        await asyncio.wait_for(checked.wait(), 1)
        service.check_pending.assert_awaited_once()
    finally:
        await service.stop()
    assert service._task is None


@pytest.mark.asyncio
async def test_failed_order_does_not_stop_next_order(monkeypatch):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = ['first', 'second']
    db.execute.return_value = result
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=db)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(polling, 'AsyncSessionLocal', lambda: context)
    payment_service = SimpleNamespace(check_atlaspay_payment_status=AsyncMock(
        side_effect=[RuntimeError('temporary'), {'is_paid': True}],
    ))
    await polling.AtlasPayPollingService().check_pending(payment_service)
    assert payment_service.check_atlaspay_payment_status.await_count == 2
    db.commit.assert_awaited_once()
