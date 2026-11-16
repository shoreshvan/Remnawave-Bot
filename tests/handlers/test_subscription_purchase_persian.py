"""Persian subscription templates and renewal receipts use real catalog values."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.subscription import pricing, purchase
from app.handlers.subscription.common import hide_subscription_device_row
from tests.fixtures.persian import persian  # noqa: F401


@pytest.fixture
def subscription():
    return SimpleNamespace(
        id=7, is_trial=False, is_active=True, tariff_id=None, connected_squads=[],
        traffic_limit_gb=0, traffic_used_gb=0, device_limit=2, autopay_enabled=False,
        end_date=datetime.now(UTC) + timedelta(days=30), subscription_url='',
        subscription_crypto_link='',
    )


@pytest.mark.parametrize('key', [
    'SUBSCRIPTION_INFO', 'SUBSCRIPTION_OVERVIEW_TEMPLATE',
    'SUBSCRIPTION_DAILY_OVERVIEW_TEMPLATE', 'SUBSCRIPTION_SETTINGS_OVERVIEW',
])
def test_optional_device_row_preserves_other_persian_content(persian, key):
    template = persian.t(key)
    result = hide_subscription_device_row(template)
    assert 'دستگاه‌ها' not in result
    assert '{devices_used}' not in result
    for line in template.splitlines():
        if 'دستگاه‌ها' not in line:
            assert line in result


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('surface', ['info', 'settings'])
async def test_actual_persian_device_visibility(persian, subscription, monkeypatch, enabled, surface):
    user = SimpleNamespace(id=1, language='fa', subscription=subscription)
    monkeypatch.setattr(type(purchase.settings), 'is_devices_selection_enabled', lambda self: enabled)
    monkeypatch.setattr(type(purchase.settings), 'is_multi_tariff_enabled', lambda self: False)
    for module in (purchase, pricing):
        monkeypatch.setattr(module, 'get_current_devices_count', AsyncMock(return_value=1))
    if surface == 'info':
        monkeypatch.setattr(pricing, '_get_countries_info', AsyncMock(return_value=[]))
        monkeypatch.setattr(pricing, 'get_subscription_cost', AsyncMock(return_value=0))
        text = await pricing.get_subscription_info_text(subscription, persian, user, AsyncMock())
    else:
        monkeypatch.setattr(purchase, '_should_show_countries_management', AsyncMock(return_value=False))
        monkeypatch.setattr(purchase, '_legacy_subscription', lambda sub: False)
        monkeypatch.setattr(purchase, 'get_updated_subscription_settings_keyboard', lambda *a, **k: None)
        callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        await purchase.handle_subscription_settings(callback, user, AsyncMock())
        text = callback.message.edit_text.await_args.args[0]
    assert ('دستگاه‌ها' in text) == enabled
    assert ('1 / 2' in text) == enabled
    assert 'ترافیک' in text
    assert '{' not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['insufficient', 'refund', 'success'])
async def test_renewal_cart_receipt_and_refund(persian, subscription, monkeypatch, outcome):
    from app.database.crud import transaction as transaction_crud, user as user_crud
    from app.utils import promo_offer

    user = SimpleNamespace(id=1, telegram_id=1, language='fa', balance_kopeks=0 if outcome == 'insufficient' else 50000)
    callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()), bot=None)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock())
    debit, refund, transaction, cart = AsyncMock(return_value=True), AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(user_crud, 'subtract_user_balance', debit)
    monkeypatch.setattr(user_crud, 'add_user_balance', refund)
    monkeypatch.setattr(transaction_crud, 'create_transaction', transaction)
    monkeypatch.setattr(purchase, '_calculate_simple_subscription_price', AsyncMock(return_value=(25000, {})))
    monkeypatch.setattr(promo_offer, 'get_user_active_promo_discount_percent', lambda user: 0)
    monkeypatch.setattr(purchase.user_cart_service, 'save_user_cart', cart)
    monkeypatch.setattr(purchase.SubscriptionService, 'update_remnawave_user', AsyncMock(return_value=True))
    monkeypatch.setattr(purchase.AdminNotificationService, 'send_subscription_extension_notification', AsyncMock())
    if outcome == 'refund':
        db.commit.side_effect = RuntimeError('database unavailable')

    await purchase._extend_existing_subscription(callback, user, db, subscription, 30, 2, 0, '')

    description = 'تمدید اشتراک به مدت 30 روز'
    if outcome == 'insufficient':
        assert cart.await_args.args[1]['description'] == description
        assert cart.await_args.args[1]['total_price'] == 25000
        debit.assert_not_awaited()
    else:
        assert debit.await_args.args[2:4] == (25000, description)
        if outcome == 'refund':
            assert refund.await_args.args[2:4] == (25000, 'بازگشت وجه به‌دلیل خطا در تمدید اشتراک')
            transaction.assert_not_awaited()
        else:
            assert transaction.await_args.kwargs['description'] == description
            assert transaction.await_args.kwargs['amount_kopeks'] == 25000
            refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_charge_receives_persian_description(persian, monkeypatch):
    from app.database.crud import server_squad
    from app.services import trial_activation_service

    user = SimpleNamespace(
        id=42, language='fa', subscription=None, has_had_paid_subscription=False,
        restriction_subscription=False, auth_type='telegram', is_trial_already_used=lambda: False,
    )
    callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    monkeypatch.setattr(trial_activation_service, 'get_trial_activation_charge_amount', lambda: 0)
    monkeypatch.setattr(type(purchase.settings), 'is_trial_disabled_for_user', lambda self, auth: False)
    monkeypatch.setattr(type(purchase.settings), 'is_tariffs_mode', lambda self: False)
    monkeypatch.setattr(type(purchase.settings), 'is_devices_selection_enabled', lambda self: True)
    monkeypatch.setattr(server_squad, 'get_random_trial_squad_uuid', AsyncMock(return_value='squad'))
    monkeypatch.setattr(purchase, 'create_trial_subscription', AsyncMock(return_value=MagicMock()))
    charge = AsyncMock(side_effect=trial_activation_service.TrialPaymentInsufficientFunds(
        required_amount=15900, balance_amount=100,
    ))
    monkeypatch.setattr(purchase, 'charge_trial_activation_if_required', charge)
    monkeypatch.setattr(purchase, 'rollback_trial_subscription_activation', AsyncMock(return_value=True))
    await purchase.activate_trial(callback, user, AsyncMock())
    assert charge.await_args.kwargs['description'] == 'فعال‌سازی اشتراک آزمایشی از طریق ربات'
    assert purchase.settings.format_price(15900) in callback.message.edit_text.await_args.args[0]
