from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users as routes
from tests.fixtures.persian import persian  # noqa: F401


@pytest.fixture
def panel(monkeypatch):
    api = AsyncMock()
    service = MagicMock()
    service.return_value.get_api_client.return_value.__aenter__.return_value = api
    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', service)
    monkeypatch.setattr(routes, 'get_user_by_id', AsyncMock(return_value=SimpleNamespace(remnawave_id=7)))
    return api


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['fa', None])
async def test_reset_device_count(panel, persian, language):
    admin = SimpleNamespace(id=1, language=language)
    panel.get_user_devices_all.return_value = {'devices': [{'hwid': 'a'}, {'hwid': 'b'}]}
    panel.remove_device.side_effect = [True, False]
    result = await routes.reset_user_devices(2, admin, None, subscription_id=None)
    assert (result.success, result.deleted_count, result.message) == (False, 1, 'از 2 دستگاه، 1 دستگاه حذف شد.')
    panel.get_user_devices_all.return_value = {'devices': []}
    result = await routes.reset_user_devices(2, admin, None, subscription_id=None)
    assert result.message == 'دستگاهی برای بازنشانی وجود ندارد'


@pytest.mark.asyncio
async def test_delete_device_results(panel, persian):
    admin = SimpleNamespace(id=1, language='fa')
    panel.remove_device.side_effect = [True, False, RuntimeError('panel unavailable')]
    for expected in ['دستگاه حذف شد', 'حذف دستگاه ناموفق بود', 'خطا در حذف دستگاه']:
        result = await routes.delete_user_device(2, 'device-id', admin, None, subscription_id=None)
        assert result.message == expected


@pytest.mark.asyncio
async def test_rename_device_missing_id(panel, persian):
    with pytest.raises(HTTPException) as error:
        await routes.rename_user_device(2, ' ', routes.RenameDeviceRequest(name='گوشی'), SimpleNamespace(language=None), None)
    assert (error.value.status_code, error.value.detail) == (400, 'شناسهٔ دستگاه را وارد کنید.')
