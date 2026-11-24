from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['fa', None])
async def test_balance_messages(monkeypatch, persian, language):
    admin = SimpleNamespace(id=1, language=language)
    user = SimpleNamespace(balance_kopeks=25025)
    db = AsyncMock()
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(admin_users, 'get_user_by_id', lookup)
    request = admin_users.UpdateBalanceRequest(amount_kopeks=-30050)

    async def update():
        return await admin_users.update_user_balance(2, request, admin, db)

    with pytest.raises(HTTPException) as missing:
        await update()
    assert missing.value.detail == 'کاربر یافت نشد'

    lookup.return_value = user
    with pytest.raises(HTTPException) as insufficient:
        await update()
    assert '250.25 تومان' in insufficient.value.detail
    assert '300.5 تومان' in insufficient.value.detail

    request.amount_kopeks = 100
    credit = AsyncMock(return_value=False)
    monkeypatch.setattr(admin_users, 'add_user_balance', credit)
    with pytest.raises(HTTPException) as failed:
        await update()
    assert failed.value.detail == 'به‌روزرسانی موجودی ناموفق بود'

    credit.return_value = True
    db.refresh.side_effect = lambda target: setattr(target, 'balance_kopeks', 25125)
    result = await update()
    assert result.message == 'موجودی از 250.25 تومان به 251.25 تومان تغییر کرد.'
