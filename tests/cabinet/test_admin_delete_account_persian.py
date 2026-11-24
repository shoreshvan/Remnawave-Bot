from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users as routes
from app.services.grace_access_runtime import GraceAccessDeletionBlocked
from app.services.user_service import DeleteUserResult, UserService
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('soft', [True, False])
async def test_delete_message(monkeypatch, persian, soft):
    monkeypatch.setattr(routes, 'get_user_by_id', AsyncMock(return_value=SimpleNamespace(id=2)))
    monkeypatch.setattr(routes, 'soft_delete_user', AsyncMock())
    guard = AsyncMock()
    monkeypatch.setattr('app.services.grace_access_runtime.ensure_no_open_grace_for_user', guard)
    admin = SimpleNamespace(id=1, language=None)
    db = AsyncMock()
    request = routes.DeleteUserRequest(soft_delete=soft)
    result = await routes.delete_user(2, request, admin, db)
    assert result.message == persian.t('CABINET_USERS_SOFT_DELETED' if soft else 'CABINET_USERS_PERMANENTLY_DELETED')
    if not soft:
        guard.side_effect = GraceAccessDeletionBlocked([123])
        with pytest.raises(HTTPException) as error:
            await routes.delete_user(2, request, admin, db)
        assert (error.value.status_code, error.value.detail) == (409, persian.t('CABINET_USERS_GRACE_MUST_DRAIN'))


@pytest.mark.asyncio
@pytest.mark.parametrize('error_key', [None, 'CABINET_USERS_PANEL_DISABLE_FAILED', 'CABINET_USERS_PANEL_DISABLED_INSTEAD'])
async def test_full_delete_panel_outcome(monkeypatch, persian, error_key):
    monkeypatch.setattr(routes, 'get_user_by_id', AsyncMock(return_value=SimpleNamespace(id=2)))
    outcome = DeleteUserResult(bot_deleted=True, panel_error='Ошибка панели' if error_key else None, panel_error_key=error_key)
    monkeypatch.setattr(UserService, 'delete_user_account', AsyncMock(return_value=outcome))
    result = await routes.full_delete_user(2, routes.FullDeleteUserRequest(), SimpleNamespace(id=1, language='fa'), AsyncMock())
    assert result.message == 'حساب کاربر برای همیشه از ربات حذف شد.'
    assert result.panel_error == (persian.t(error_key) if error_key else None)
