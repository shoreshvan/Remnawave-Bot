"""Real purchase result and receipts, with isolated financial and panel IO."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import subscription_purchase_service as purchase
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('discount', [0, 5000])
async def test_purchase_result_and_receipts(persian, monkeypatch, discount):
    from app.database.crud import subscription as subscription_crud, transaction as transaction_crud, user as user_crud
    from app.services import traffic_base_grant_service

    user = SimpleNamespace(id=1, language='fa', balance_kopeks=100000, remnawave_id='panel')
    subscription = SimpleNamespace(id=7, end_date=datetime.now(UTC) + timedelta(days=30))
    context = SimpleNamespace(user=user, subscription=None, payload={})
    pricing = SimpleNamespace(
        final_total=25000 - discount, base_original_total=25000, promo_discount_value=discount,
        promo_discount_percent=20 if discount else 0, months=1, server_ids=[],
        selection=SimpleNamespace(period=SimpleNamespace(days=30), traffic_value=100, devices=2, servers=[]),
    )
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    debit, transaction, emit = AsyncMock(return_value=True), AsyncMock(return_value=SimpleNamespace(id=8)), AsyncMock()
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(purchase, 'subtract_user_balance', debit)
    monkeypatch.setattr(purchase, 'create_paid_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(subscription_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    monkeypatch.setattr(purchase, 'create_transaction', transaction)
    monkeypatch.setattr(transaction_crud, 'emit_transaction_side_effects', emit)
    monkeypatch.setattr(traffic_base_grant_service, 'stage_paid_base_grant', AsyncMock())
    monkeypatch.setattr(type(purchase.settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(purchase.settings, 'CURRENCY_SYMBOL', 'تومان')
    panel = SimpleNamespace(update_remnawave_user=AsyncMock(return_value=True))
    monkeypatch.setattr(purchase, 'SubscriptionService', lambda: panel)

    result = await purchase.MiniAppSubscriptionPurchaseService().submit_purchase(db, context, pricing)

    assert debit.await_args.args[2:4] == (25000 - discount, 'خرید اشتراک به مدت 30 روز')
    for call in (transaction.await_args, emit.await_args):
        assert call.kwargs['description'] == 'اشتراک 30 روزه (1 ماه)'
        assert call.kwargs['amount_kopeks'] == 25000 - discount
    expected = '🎉 اشتراک خریداری شد!'
    if discount:
        expected += '\n\n' + persian.t('SUBSCRIPTION_PROMO_DISCOUNT_NOTE').format(
            percent=20, amount=persian.format_price(5000),
        )
    assert result['message'] == expected
    assert result['subscription'] is subscription
    assert '₽' not in result['message']
    db.rollback.assert_not_awaited()
