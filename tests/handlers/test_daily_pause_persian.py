"""Render daily subscription alerts using Persian without locale fallback."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.crud import subscription as subscription_crud, tariff as tariff_crud, user as user_crud
from app.handlers.subscription import purchase
from app.services import traffic_purchase_service
from app.services.pricing_engine import PricingEngine
from tests.fixtures.persian import persian  # noqa: F401


@pytest.fixture
def daily(monkeypatch, persian):
    subscription = SimpleNamespace(
        id=7, traffic_accounting_mode='legacy', is_trial=False, tariff_id=3,
        is_daily_paused=True, status='disabled', connected_squads=['squad'],
    )
    user = SimpleNamespace(
        id=1, language='fa', subscription=subscription, restriction_subscription=False,
        balance_kopeks=0, remnawave_id='panel-id',
    )
    tariff = SimpleNamespace(id=3, name='روزانه', is_daily=True)
    callback = SimpleNamespace(answer=AsyncMock())
    db = SimpleNamespace(rollback=AsyncMock(), refresh=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(type(purchase.settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(purchase.settings, 'CURRENCY_SYMBOL', 'تومان')
    lock = AsyncMock(return_value=subscription)
    monkeypatch.setattr(traffic_purchase_service, 'lock_daily_accounting_subscription', lock)
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(PricingEngine, 'daily_subscription_price', lambda *args: 12500)
    debit = AsyncMock(return_value=False)
    monkeypatch.setattr(user_crud, 'subtract_user_balance', debit)
    return SimpleNamespace(user=user, subscription=subscription, callback=callback, db=db, lock=lock, debit=debit)


@pytest.mark.asyncio
@pytest.mark.parametrize(('error', 'key'), [
    ('Daily subscription ownership changed', 'DAILY_SUBSCRIPTION_ACCESS_CHANGED'),
    ('Daily resume is unavailable during grace', 'DAILY_SUBSCRIPTION_GRACE_ACTIVE'),
    ('Daily resume awaits completed migration', 'DAILY_SUBSCRIPTION_MIGRATION_PENDING'),
    ('Сначала отключите автоплатёж CisPay и дождитесь подтверждения отключения.', 'DAILY_SUBSCRIPTION_CISPAY_CONFLICT'),
    ('unexpected internal detail', 'DAILY_SUBSCRIPTION_STATE_ERROR'),
])
async def test_lock_errors_are_actionable_persian(daily, persian, error, key):
    daily.lock.side_effect = ValueError(error)
    await purchase.handle_toggle_daily_subscription_pause(daily.callback, daily.user, daily.db)
    daily.callback.answer.assert_awaited_once_with(persian.t(key), show_alert=True)
    daily.db.rollback.assert_awaited_once()
    daily.debit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(('attribute', 'key'), [
    ('is_trial', 'DAILY_SUBSCRIPTION_TRIAL_UNAVAILABLE'),
    ('restriction_subscription', 'DAILY_SUBSCRIPTION_RESUME_RESTRICTED'),
])
async def test_restriction_alerts(daily, persian, attribute, key):
    target = daily.subscription if attribute == 'is_trial' else daily.user
    setattr(target, attribute, True)
    await purchase.handle_toggle_daily_subscription_pause(daily.callback, daily.user, daily.db)
    daily.callback.answer.assert_awaited_once_with(persian.t(key), show_alert=True)
    daily.debit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('balance', [0, 12500])
async def test_both_insufficient_balance_paths_render_amount(daily, balance):
    daily.user.balance_kopeks = balance
    await purchase.handle_toggle_daily_subscription_pause(daily.callback, daily.user, daily.db)
    daily.callback.answer.assert_awaited_once_with(
        f'❌ موجودی برای ازسرگیری کافی نیست. مبلغ لازم: {purchase.settings.format_price(12500)}',
        show_alert=True,
    )
    if balance:
        daily.debit.assert_awaited_once()
        assert daily.debit.await_args.args[2] == 12500
        assert daily.debit.await_args.args[3] == 'پرداخت روزانهٔ تعرفهٔ «روزانه» (از سرگیری)'
    else:
        daily.debit.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_resume_preserves_charge_and_translates_transaction(daily, persian, monkeypatch):
    from app.database.crud import transaction as transaction_crud
    from app.services.subscription_service import SubscriptionService
    from app.services import traffic_reset_policy

    daily.user.balance_kopeks = 12500
    daily.debit.return_value = True
    create = AsyncMock()
    monkeypatch.setattr(transaction_crud, 'create_transaction', create)
    monkeypatch.setattr(subscription_crud, 'resume_daily_subscription', AsyncMock(return_value=daily.subscription))
    monkeypatch.setattr(traffic_reset_policy, 'should_reset_traffic_on_daily_charge', lambda tariff: False)
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', AsyncMock())
    monkeypatch.setattr(purchase.settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(purchase, 'show_subscription_info', AsyncMock())

    await purchase.handle_toggle_daily_subscription_pause(daily.callback, daily.user, daily.db)

    assert create.await_args.kwargs['amount_kopeks'] == 12500
    assert create.await_args.kwargs['description'] == daily.debit.await_args.args[3]
    daily.callback.answer.assert_awaited_once_with(persian.t('DAILY_SUBSCRIPTION_RESUMED'), show_alert=True)


@pytest.mark.asyncio
async def test_pause_success_and_missing_subscription(daily, persian, monkeypatch):
    daily.subscription.is_daily_paused = False
    daily.subscription.status = 'active'
    monkeypatch.setattr(subscription_crud, 'toggle_daily_subscription_pause', AsyncMock(return_value=daily.subscription))
    monkeypatch.setattr(purchase, 'show_subscription_info', AsyncMock())
    await purchase.handle_toggle_daily_subscription_pause(daily.callback, daily.user, daily.db)
    daily.callback.answer.assert_awaited_once_with(persian.t('DAILY_SUBSCRIPTION_PAUSED'), show_alert=True)
    daily.debit.assert_not_awaited()

    daily.callback.answer.reset_mock()
    daily.user.subscription = None
    await purchase.handle_toggle_daily_subscription_pause(daily.callback, daily.user, daily.db)
    daily.callback.answer.assert_awaited_once_with(persian.t('NO_SUBSCRIPTION_ERROR'), show_alert=True)


@pytest.mark.parametrize('currency', ['تومان', 'واحد آزمایشی'])
def test_traffic_switch_price_and_discount_use_configured_currency(persian, monkeypatch, currency):
    from app.handlers.subscription.common import get_traffic_switch_keyboard

    monkeypatch.setattr(purchase.settings, 'CURRENCY_SYMBOL', currency)
    monkeypatch.setattr(type(purchase.settings), 'get_traffic_packages', lambda self: [
        {'gb': 10, 'price': 10000, 'enabled': True},
        {'gb': 50, 'price': 30000, 'enabled': True},
    ])
    monkeypatch.setattr(type(purchase.settings), 'get_traffic_price', lambda self, gb: 10000)
    markup = get_traffic_switch_keyboard(10, 'fa', discount_percent=10)
    labels = {button.callback_data: button.text for row in markup.inline_keyboard for button in row}
    assert f'(+180 {currency})' in labels['switch_traffic_50']
    assert f'تخفیف 10٪: -20 {currency}' in labels['switch_traffic_50']
    assert all('₽' not in label for label in labels.values())
