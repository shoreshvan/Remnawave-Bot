from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.webapi.routes import miniapp
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('reason', 'status', 'message'), [
    ('expired', 410, 'مهلت کد تخفیف به پایان رسیده است'),
    ('trial_subscription_exists', 409, 'شما در حال حاضر اشتراک دارید، بنابراین این کد آزمایشی قابل اعمال نیست'),
    ('unrecognized', 400, 'فعال‌سازی کد تخفیف ممکن نشد'),
])
async def test_promocode_failure_without_account_language(monkeypatch, persian, reason, status, message):
    monkeypatch.setattr(miniapp, 'parse_webapp_init_data', lambda *_: {'user': {'id': 1}})
    monkeypatch.setattr(miniapp, 'get_user_by_telegram_id', AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(miniapp.promo_code_service, 'activate_promocode', AsyncMock(return_value={'error': reason}))
    with pytest.raises(HTTPException) as caught:
        await miniapp.activate_promo_code(SimpleNamespace(init_data='test', code='VIP', subscription_id=None), AsyncMock())
    assert caught.value.status_code == status
    assert caught.value.detail == {'code': reason, 'message': message}


@pytest.mark.asyncio
@pytest.mark.parametrize(('reason', 'expected'), [
    ('subscription_missing', 'اشتراک فعال لازم است'),
    ('squads_missing', 'برای این پیشنهاد هنوز گروه سروری تعیین نشده است'),
    ('already_connected', 'سرورها از قبل متصل شده‌اند'),
    ('remnawave_sync_failed', 'فعال‌سازی دسترسی به سرورها انجام نشد'),
    ('unknown', 'فعال‌سازی پیشنهاد ممکن نشد'),
])
async def test_test_access_offer_error_in_persian(monkeypatch, persian, reason, expected):
    from datetime import UTC, datetime, timedelta

    user = SimpleNamespace(id=1)
    offer = SimpleNamespace(user_id=1, claimed_at=None, is_active=True, effect_type='test_access',
                            expires_at=datetime.now(UTC) + timedelta(hours=1))
    monkeypatch.setattr(miniapp, 'parse_webapp_init_data', lambda *_: {'user': {'id': 1}})
    monkeypatch.setattr(miniapp, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(miniapp, 'get_offer_by_id', AsyncMock(return_value=offer))
    monkeypatch.setattr(miniapp.promo_offer_service, 'grant_test_access', AsyncMock(return_value=(False, [], None, reason)))
    with pytest.raises(HTTPException) as caught:
        await miniapp.claim_promo_offer(1, SimpleNamespace(init_data='test'), AsyncMock())
    assert caught.value.status_code == 400
    assert caught.value.detail == {'code': reason, 'message': expected}
