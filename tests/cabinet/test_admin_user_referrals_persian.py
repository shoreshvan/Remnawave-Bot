from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users as routes
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['fa', None])
async def test_restrictions_and_promo_group(monkeypatch, persian, language):
    admin = SimpleNamespace(id=1, language=language)
    user = SimpleNamespace(restriction_topup=False, restriction_subscription=False, promo_group_id=None)
    db = AsyncMock()
    monkeypatch.setattr(routes, 'get_user_by_id', AsyncMock(return_value=user))
    request = routes.UpdateRestrictionsRequest(restriction_topup=True, restriction_reason='دلیل ثبت‌شدهٔ مدیر')
    result = await routes.update_user_restrictions(2, request, admin, db)
    assert result.message == persian.t('CABINET_USERS_RESTRICTIONS_UPDATED')
    assert result.restriction_reason == request.restriction_reason

    db.execute.return_value = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    with pytest.raises(HTTPException) as error:
        await routes.update_user_promo_group(2, routes.UpdatePromoGroupRequest(promo_group_id=3), admin, db)
    assert error.value.detail == 'گروه تخفیف پیدا نشد.'


@pytest.mark.asyncio
async def test_referral_messages(monkeypatch, persian):
    admin = SimpleNamespace(id=1, language='fa')
    user = SimpleNamespace(referred_by_id=None)
    db = AsyncMock()
    monkeypatch.setattr(routes, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(routes, '_would_create_referral_cycle', AsyncMock(return_value=False))
    monkeypatch.setattr(routes.PermissionService, 'log_action', AsyncMock())
    with pytest.raises(HTTPException) as error:
        await routes.update_user_referral_commission(1, routes.UpdateReferralCommissionRequest(commission_percent=5), admin, db)
    assert error.value.detail == persian.t('CABINET_USERS_ADMIN_CANNOT_MODIFY_OWN_COMMISSION')

    result = await routes.assign_user_referrer(2, routes.AssignReferrerRequest(referrer_id=3), admin, db)
    assert result.message == 'معرف با موفقیت تعیین شد. پاداش‌ها در شارژ بعدی کاربر اعمال می‌شوند.'
    result = await routes.remove_user_referral(3, 2, admin, db)
    assert result.message == 'کاربر از زیرمجموعهٔ این معرف خارج شد.'
    with pytest.raises(HTTPException) as error:
        await routes.remove_user_referrer(2, admin, db)
    assert error.value.detail == persian.t('CABINET_USERS_NO_REFERRER')
