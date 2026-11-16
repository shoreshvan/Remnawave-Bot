"""Shared renewal receipts follow the recipient language without fallback."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.crud import subscription as subscription_crud, tariff as tariff_crud, user as user_crud
from app.services import subscription_auto_purchase_service as auto, subscription_renewal_service as renewal
from app.services.pricing_engine import pricing_engine
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['classic', 'tariff', 'missing_tariff', 'custom'])
async def test_saved_cart_default_receipt(persian, monkeypatch, kind):
    from app.utils import promo_offer

    user = SimpleNamespace(id=1, language='fa', restriction_subscription=False)
    subscription = SimpleNamespace(id=7, tariff_id=3 if kind in ('tariff', 'missing_tariff') else None)
    tariff = SimpleNamespace(name='ویژه', is_active=True, is_daily=False, period_prices={'30': 25000})
    monkeypatch.setattr(type(auto.settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(auto.settings), 'is_tariffs_mode', lambda self: False)
    monkeypatch.setattr(type(auto.settings), 'get_available_renewal_periods', lambda self: [30])
    monkeypatch.setattr(subscription_crud, 'get_subscription_by_user_id', AsyncMock(return_value=subscription))
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=None if kind == 'missing_tariff' else tariff))
    monkeypatch.setattr(pricing_engine, 'calculate_renewal_price', AsyncMock(return_value=SimpleNamespace(
        final_total=25000, original_total=30000,
    )))
    monkeypatch.setattr(promo_offer, 'get_user_active_promo_discount_percent', lambda user: 0)
    cart = {'period_days': 30}
    if subscription.tariff_id:
        cart['tariff_id'] = subscription.tariff_id
    if kind == 'custom':
        cart['description'] = 'شرح سفارشی حفظ شود'
    db = SimpleNamespace(scalar=AsyncMock(return_value=subscription))
    context = await auto._prepare_auto_extend_context(db, user, cart)
    expected = {
        'classic': 'تمدید اشتراک به مدت 30 روز',
        'tariff': 'تمدید تعرفهٔ «ویژه» به مدت 30 روز',
        'missing_tariff': 'تمدید تعرفهٔ «تعرفه» به مدت 30 روز',
        'custom': 'شرح سفارشی حفظ شود',
    }
    assert context.description == expected[kind]
    assert context.price_kopeks == 25000
    assert context.period_days == 30


@pytest.mark.asyncio
@pytest.mark.parametrize('custom', [None, 'شرح اختصاصی تمدید'])
@pytest.mark.parametrize('charge', [0, 15000])
async def test_shared_renewal_receipt_reaches_debit_transaction_and_notifications(persian, monkeypatch, custom, charge):
    from app.database.crud import transaction as transaction_crud
    from app.services.payment import lava

    user = SimpleNamespace(id=1, language='fa', remnawave_id='panel', balance_kopeks=50000)
    subscription = SimpleNamespace(id=7, user_id=1, end_date=datetime.now(UTC))
    pricing = SimpleNamespace(final_total=25000, period_days=30, promo_offer_discount=0, breakdown={})
    result = MagicMock()
    result.scalar_one.return_value = subscription
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock())
    debit, create, emit = AsyncMock(return_value=True), AsyncMock(), AsyncMock()
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(renewal, 'subtract_user_balance', debit)
    monkeypatch.setattr(renewal, 'extend_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(renewal, 'create_transaction', create)
    monkeypatch.setattr(transaction_crud, 'emit_transaction_side_effects', emit)
    monkeypatch.setattr(lava, 'shift_lava_next_charge_after_manual_extension', AsyncMock())
    monkeypatch.setattr(renewal.SubscriptionService, 'update_remnawave_user', AsyncMock(return_value=True))
    monkeypatch.setattr(renewal, 'with_admin_notification_service', AsyncMock())
    monkeypatch.setattr(type(renewal.settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(renewal.settings, 'RESET_DEVICES_ON_RENEWAL', False)

    result = await renewal.SubscriptionRenewalService().finalize(
        db, user, subscription, pricing, charge_balance_amount=charge, description=custom,
    )

    expected = custom or 'تمدید اشتراک به مدت 30 روز'
    assert create.await_args.kwargs['description'] == expected
    assert emit.await_args.kwargs['description'] == expected
    assert create.await_args.kwargs['amount_kopeks'] == 25000
    if charge:
        assert debit.await_args.args[2:4] == (15000, expected)
    else:
        debit.assert_not_awaited()
    assert result.total_amount_kopeks == 25000
    assert result.charged_from_balance_kopeks == charge
    db.rollback.assert_not_awaited()
