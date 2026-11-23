from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher

from app.database.models import PaymentMethod
from app.handlers.admin import payments
from app.services.payment import manual_credit as mc


METHODS = list(mc.IRANIAN_FORCE_CREDIT_METHODS)


@pytest.fixture
def payment():
    return SimpleNamespace(id=1, order_id='order', is_paid=False, transaction_id=None,
                           user_id=2, amount_kopeks=5000000, status='pending',
                           metadata_json={}, processed_events=[])


@pytest.mark.asyncio
@pytest.mark.parametrize('method', METHODS)
async def test_credit_audited_and_repeated_click_does_not_credit_twice(monkeypatch, payment, method):
    monkeypatch.setattr(mc.settings, 'ADMIN_IDS', '99')
    crud = import_module(f'app.database.crud.{method.value}')
    lock = AsyncMock(return_value=payment)
    monkeypatch.setattr(crud, f'get_{method.value}_payment_by_id_for_update', lock)
    finalize = AsyncMock(return_value=True)
    service = SimpleNamespace(**{f'_finalize_{method.value}_payment': finalize})
    db = AsyncMock()
    for _ in range(2):
        assert await mc.force_credit_iranian_payment(db, service, method, 1, admin_id=99) is payment
    finalize.assert_awaited_once_with(db, payment, trigger='admin_force')
    assert lock.await_count == 2
    assert payment.metadata_json['manual_credit']['admin_telegram_id'] == 99
    assert payment.metadata_json['manual_credit']['previous_status'] == 'pending'
    assert payment.metadata_json['manual_credit']['provider_verified'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('status', ['refunded', 'failed', 'expired', 'rejected', 'cancelled', 'success'])
async def test_terminal_status_refused_under_lock(monkeypatch, payment, method, status):
    monkeypatch.setattr(mc.settings, 'ADMIN_IDS', '99')
    payment.status = status
    crud = import_module(f'app.database.crud.{method.value}')
    monkeypatch.setattr(crud, f'get_{method.value}_payment_by_id_for_update', AsyncMock(return_value=payment))
    assert await mc.force_credit_iranian_payment(AsyncMock(), object(), method, 1, admin_id=99) is None
    assert not payment.is_paid


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [False, RuntimeError('failure')])
async def test_failed_finalization_rolls_back(monkeypatch, payment, outcome):
    monkeypatch.setattr(mc.settings, 'ADMIN_IDS', '99')
    crud = import_module('app.database.crud.atlaspay')
    monkeypatch.setattr(crud, 'get_atlaspay_payment_by_id_for_update', AsyncMock(return_value=payment))
    finalize = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
    db = AsyncMock()
    service = SimpleNamespace(_finalize_atlaspay_payment=finalize)
    if isinstance(outcome, Exception):
        with pytest.raises(RuntimeError):
            await mc.force_credit_iranian_payment(db, service, PaymentMethod.ATLASPAY, 1, admin_id=99)
    else:
        assert await mc.force_credit_iranian_payment(db, service, PaymentMethod.ATLASPAY, 1, admin_id=99) is None
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_admin_rejected_before_database_access(monkeypatch):
    monkeypatch.setattr(mc.settings, 'ADMIN_IDS', '')
    assert await mc.force_credit_iranian_payment(None, None, PaymentMethod.ATLASPAY, 1, admin_id=99) is None


@pytest.mark.parametrize('field,value', [('user_id', None), ('transaction_id', 7), ('amount_kopeks', 0)])
def test_ineligible_invoice_not_offered(payment, field, value):
    setattr(payment, field, value)
    assert not mc.can_force_credit(PaymentMethod.ATLASPAY, payment)


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix,expected', [
    ('force', payments.force_credit_payment_prompt),
    ('forceyes', payments.force_credit_payment_yes),
    ('forcecancel', payments.force_credit_payment_cancel),
])
async def test_button_dispatches_to_correct_handler(prefix, expected):
    dp = Dispatcher()
    payments.register_handlers(dp)
    event = SimpleNamespace(data=f'admin_payment_{prefix}_atlaspay_1')
    for handler in dp.callback_query.handlers:
        matches, _ = await handler.check(event)
        if matches:
            assert handler.callback is expected
            break
    else:
        pytest.fail('No matching handler')


@pytest.mark.asyncio
@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('status', ['manual_review', 'amount_mismatch'])
async def test_manual_review_visible_to_admin_but_not_background_checker(monkeypatch, payment, method, status):
    from app.services import payment_verification_service as pvs

    payment.status = status
    payment.expires_at = None
    result = MagicMock()
    result.scalars.return_value.all.return_value = [payment]
    db = AsyncMock()
    db.execute.return_value = result
    monkeypatch.setattr(pvs, '_build_record', lambda *args, **kwargs: payment)
    fetch = getattr(pvs, f'_fetch_{method.value}_payments')
    assert await fetch(db, datetime.now(UTC)) == []
    assert await fetch(db, datetime.now(UTC), include_manual_review=True) == [payment]
