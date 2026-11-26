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
