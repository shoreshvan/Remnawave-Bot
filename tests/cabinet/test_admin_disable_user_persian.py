from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users
from app.services.subscription_service import SubscriptionService
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('exists', [True, False])
async def test_disable_rejected(monkeypatch, persian, exists):
    user = SimpleNamespace(id=2) if exists else None
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.services.rbac_bootstrap_service.is_protected_from_blocking', lambda _: True)
    db = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await admin_users.disable_user(2, admin=SimpleNamespace(id=1, language='fa'), db=db)
    expected = 'CABINET_USERS_ACCOUNT_PROTECTED_FROM_BLOCK' if exists else 'CABINET_USERS_NOT_FOUND'
    assert error.value.detail == persian.t(expected)
    assert error.value.status_code == (403 if exists else 404)
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('panel_failure', [False, True])
async def test_disable_result(monkeypatch, persian, panel_failure):
    user = SimpleNamespace(id=2, subscriptions=[], remnawave_id=7)
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.services.rbac_bootstrap_service.is_protected_from_blocking', lambda _: False)
    monkeypatch.setattr(type(admin_users.settings), 'is_multi_tariff_enabled', lambda _: False)
    disable = AsyncMock(return_value=True, side_effect=RuntimeError('panel offline') if panel_failure else None)
    monkeypatch.setattr(SubscriptionService, 'disable_remnawave_user', disable)
    result = await admin_users.disable_user(2, admin=SimpleNamespace(id=1, language=None), db=AsyncMock())
    assert result.message == 'کاربر با موفقیت غیرفعال شد'
    assert result.user_blocked
    assert result.panel_deactivated == (not panel_failure)
    assert result.panel_error == ('غیرفعال‌سازی حساب در پنل انجام نشد.' if panel_failure else None)
