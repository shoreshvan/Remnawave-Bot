from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.webapi.routes import miniapp
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('panel_id', 'hwid', 'status', 'code', 'message'), [
    (None, 'abc', 409, 'remnawave_unavailable', 'کاربر RemnaWave متصل نشده است'),
    (1, '', 400, 'invalid_hwid', 'شناسه دستگاه الزامی است'),
    (1, 'abc', 503, 'service_unavailable', 'مدیریت دستگاه‌ها موقتاً در دسترس نیست'),
])
async def test_device_removal_errors(monkeypatch, persian, panel_id, hwid, status, code, message):
    user = SimpleNamespace(remnawave_id=panel_id)
    monkeypatch.setattr(miniapp, 'parse_webapp_init_data', lambda *_: {'user': {'id': 1}})
    monkeypatch.setattr(miniapp, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(miniapp, 'RemnaWaveService', lambda: SimpleNamespace(is_configured=False))
    with pytest.raises(HTTPException) as caught:
        await miniapp.remove_connected_device(SimpleNamespace(init_data='test', hwid=hwid), AsyncMock())
    assert caught.value.status_code == status
    assert caught.value.detail == {'code': code, 'message': message}


def test_transaction_history_localizes_legacy_description(persian):
    transaction = SimpleNamespace(
        id=1, type='deposit', amount_kopeks=12345, description='Бонус по промокоду VIP-25',
        payment_method=None, external_id=None, is_completed=True, created_at=datetime.now(UTC), completed_at=None,
    )
    result = miniapp._serialize_transaction(transaction, 'fa')
    assert result.description == 'پاداش کد تخفیف VIP-25'
    assert result.amount_kopeks == 12345
    assert transaction.description == 'Бонус по промокоду VIP-25'


@pytest.mark.parametrize(('suffix', 'expected'), [('', ''), (' за 12 дн.', ' برای 12 روز')])
def test_legacy_device_change_receipt(persian, suffix, expected):
    stored = 'Изменение количества устройств с 2 до 4' + suffix
    assert miniapp.localize_transaction_description(stored, 'fa') == 'تغییر تعداد دستگاه‌ها از 2 به 4' + expected


@pytest.mark.asyncio
@pytest.mark.parametrize(('requested', 'expected'), [
    (None, 'محدودیت دستگاه الزامی است'),
    (0, 'محدودیت دستگاه باید مثبت باشد'),
])
async def test_device_limit_validation_for_unset_language(monkeypatch, persian, requested, expected):
    subscription = SimpleNamespace(id=3, is_active=True, is_trial=False, actual_status='active')
    user = SimpleNamespace(subscriptions=[subscription])
    monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
    payload = SimpleNamespace(init_data='test', subscription_id=3, devices=requested, device_limit=None)
    with pytest.raises(HTTPException) as caught:
        await miniapp.update_subscription_devices_endpoint(payload, AsyncMock())
    assert caught.value.status_code == 400
    assert caught.value.detail == {'code': 'validation_error', 'message': expected}


@pytest.mark.asyncio
async def test_shared_authorization_errors_are_persian(monkeypatch, persian):
    with pytest.raises(HTTPException) as missing:
        await miniapp._authorize_miniapp_user('', AsyncMock())
    assert missing.value.detail == {'code': 'unauthorized', 'message': 'داده‌های احراز هویت موجود نیست'}

    monkeypatch.setattr(miniapp, 'parse_webapp_init_data', lambda *_: {'user': {'id': 1}})
    monkeypatch.setattr(miniapp, 'get_user_by_telegram_id', AsyncMock(return_value=SimpleNamespace(status='blocked')))
    with pytest.raises(HTTPException) as blocked:
        await miniapp._authorize_miniapp_user('test', AsyncMock())
    assert blocked.value.detail == {'code': 'account_blocked', 'message': 'حساب مسدود یا حذف شده است'}


def test_renewal_messages_show_exact_toman_and_gateway(persian, monkeypatch):
    user = SimpleNamespace(language=None)
    subscription = SimpleNamespace(end_date=None, tariff=None)
    monkeypatch.setattr(type(miniapp.settings), 'is_multi_tariff_enabled', lambda self: False)
    assert miniapp._build_renewal_success_message(user, subscription, 12345) == (
        'اشتراک با موفقیت تمدید شد. مبلغ 123.45 تومان کسر شد.'
    )
    assert miniapp._build_renewal_pending_message(user, 12345, 'tonpays') == (
        'موجودی کافی نیست. برای تکمیل تمدید، 123.45 تومان را از طریق تون‌پی پرداخت کنید.'
    )
    assert miniapp._build_renewal_status_message(user) == persian.t('MINIAPP_RENEWAL_PRICE_HINT')


@pytest.mark.asyncio
async def test_renewal_periods_use_persian_for_missing_language(monkeypatch, persian):
    from app.services.pricing_engine import pricing_engine

    user = SimpleNamespace(language=None)
    tariff = SimpleNamespace(id=1, period_prices={'30': 12345})
    subscription = SimpleNamespace(id=1, tariff_id=1)
    result = SimpleNamespace(original_total=12345, final_total=12345, promo_offer_discount=0, is_tariff_mode=True)
    monkeypatch.setattr(miniapp, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(pricing_engine, 'calculate_renewal_price', AsyncMock(return_value=result))
    periods, prices, recommended = await miniapp._prepare_subscription_renewal_options(AsyncMock(), user, subscription)
    assert periods[0].title == '1 ماه'
    assert periods[0].price_kopeks == 12345
    assert prices[30]['final_total'] == 12345
    assert recommended == periods[0].id


@pytest.mark.asyncio
async def test_traffic_validation_uses_persian(monkeypatch, persian):
    subscription = SimpleNamespace(id=3, is_active=True, is_trial=False, actual_status='active', traffic_limit_gb=10)
    monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=SimpleNamespace(subscriptions=[subscription])))
    payload = SimpleNamespace(init_data='test', subscription_id=3, traffic=-1, traffic_gb=None)
    with pytest.raises(HTTPException) as caught:
        await miniapp.update_subscription_traffic_endpoint(payload, AsyncMock())
    assert caught.value.detail['message'] == 'مقدار ترافیک نباید منفی باشد'


def test_legacy_traffic_switch_receipt(persian):
    description = 'Переключение трафика с 10GB на 20GB за 15 дн.'
    assert miniapp.localize_transaction_description(description, 'fa') == (
        'تغییر ترافیک از 10 گیگابایت به 20 گیگابایت برای 15 روز'
    )


@pytest.mark.asyncio
async def test_empty_server_selection_is_persian(monkeypatch, persian):
    subscription = SimpleNamespace(id=3, is_active=True, is_trial=False, actual_status='active', connected_squads=[])
    monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=SimpleNamespace(subscriptions=[subscription])))
    payload = SimpleNamespace(init_data='test', subscription_id=3, servers=[], squads=[], server_uuids=[], squad_uuids=[])
    with pytest.raises(HTTPException) as caught:
        await miniapp.update_subscription_servers_endpoint(payload, AsyncMock())
    assert caught.value.detail['message'] == 'حداقل یک سرور باید انتخاب شود'


def test_legacy_server_receipt_in_persian(persian):
    assert miniapp.localize_transaction_description('Добавление серверов: اروپا, آسیا за 15 дн.', 'fa') == (
        'افزودن سرورها: اروپا, آسیا برای 15 روز'
    )
