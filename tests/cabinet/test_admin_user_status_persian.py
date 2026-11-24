from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users
from app.services.user_service import UserService
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('old', 'new', 'expected'),
    [
        ('active', 'blocked', 'وضعیت از ✅ فعال به 🚫 مسدود تغییر کرد'),
        ('blocked', 'deleted', 'وضعیت از 🚫 مسدود به 🗑️ حذف‌شده تغییر کرد'),
        ('legacy', 'active', 'وضعیت از ❓ نامشخص به ✅ فعال تغییر کرد'),
        ('active', 'active', 'وضعیت بدون تغییر'),
    ],
)
async def test_status_message(monkeypatch, persian, old, new, expected):
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=SimpleNamespace(status=old)))
    result = await admin_users.update_user_status(
        2, admin_users.UpdateUserStatusRequest(status=new), SimpleNamespace(id=1, language=None), AsyncMock()
    )
    assert (result.old_status, result.new_status, result.message) == (old, new, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('route', 'success_key', 'error_key'),
    [
        (admin_users.block_user, 'CABINET_USERS_USER_BLOCKED', 'CABINET_USERS_NOT_FOUND_OR_BLOCK_FAILED'),
        (admin_users.unblock_user, 'CABINET_USERS_USER_UNBLOCKED', 'CABINET_USERS_NOT_FOUND_OR_UNBLOCK_FAILED'),
    ],
)
async def test_block_messages(monkeypatch, persian, route, success_key, error_key):
    monkeypatch.setattr(UserService, route.__name__, AsyncMock(side_effect=[True, False]))
    admin = SimpleNamespace(id=1, language='fa')
    result = await route(2, admin=admin, db=None)
    assert result.message == persian.t(success_key)
    with pytest.raises(HTTPException) as error:
        await route(2, admin=admin, db=None)
    assert (error.value.status_code, error.value.detail) == (404, persian.t(error_key))
