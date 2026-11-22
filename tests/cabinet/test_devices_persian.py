import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import devices
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'key', 'code'), [
    ('none', 'PRICE_NO_SUBSCRIPTION', 'no_active_subscription'),
    ('unlimited', 'UNLIMITED_NO_PURCHASE', 'devices_unavailable'),
    ('unavailable', 'PURCHASE_UNAVAILABLE', 'devices_unavailable'),
    ('max', 'PRICE_MAX_REACHED', 'max_devices_reached'),
    ('partial', 'PRICE_CAN_ADD_MAX', 'can_add_limited'),
])
async def test_price_reasons(persian, monkeypatch, case, key, code):
    sub = None if case == 'none' else SimpleNamespace(status='active', tariff_id=None,
        device_limit=0 if case == 'unlimited' else 3 if case == 'max' else 1)
    monkeypatch.setattr(devices, 'resolve_subscription', AsyncMock(return_value=sub))
    monkeypatch.setattr(devices, 'ensure_subscription_has_tariff', lambda *args, **kwargs: None)
    monkeypatch.setattr(devices.settings, 'PRICE_PER_DEVICE', 0 if case == 'unavailable' else 100)
    monkeypatch.setattr(devices.settings, 'MAX_DEVICES_LIMIT', 3)
    result = await devices.get_device_price(devices=3, subscription_id=None,
        user=SimpleNamespace(language='fa'), db=AsyncMock())
    assert result['reason'] == persian.t('CABINET_DEVICES_' + key).format(limit=3, count=2)
    assert result['reason_code'] == code and result['available'] is False
    if case == 'partial':
        assert result['can_add'] == 2 and result['max_device_limit'] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['get_devices', 'delete_device', 'delete_all_devices'])
async def test_missing_subscription(persian, monkeypatch, name):
    monkeypatch.setattr(devices, 'resolve_subscription', AsyncMock(return_value=None))
    kwargs = dict(subscription_id=None, user=SimpleNamespace(language='fa'), db=AsyncMock())
    if name == 'delete_device':
        kwargs['hwid'] = 'unchanged-id'
    with pytest.raises(HTTPException) as error:
        await getattr(devices, name)(**kwargs)
    assert error.value.status_code == 404
    assert error.value.detail == persian.t('CABINET_DEVICES_NO_SUBSCRIPTION_FOUND')


@pytest.mark.parametrize('count', [0, 1, 5])
def test_dynamic_display_expressions(persian, count):
    path = Path(devices.__file__)
    tree = ast.parse(path.read_text(encoding='utf-8'))
    context = dict(get_texts=devices.get_texts, user=SimpleNamespace(language='fa'),
        request=SimpleNamespace(devices=count), devices_discount_percent=15, max_device_limit=count,
        min_device_limit=count, can_add=count, total=count, devices_removed_count=count)
    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != 'format':
            continue
        call = node.func.value
        if not isinstance(call, ast.Call) or not call.args or not isinstance(call.args[0], ast.Constant):
            continue
        key = call.args[0].value
        if not isinstance(key, str) or not key.startswith('CABINET_DEVICES_'):
            continue
        rendered = eval(compile(ast.Expression(node), str(path), 'eval'), context)
        assert str(count) in rendered
        assert any('\u0600' <= c <= '\u06ff' for c in rendered)
        assert not any('\u0400' <= c <= '\u04ff' for c in rendered)
        assert '₽' not in rendered
        seen.add(key)
    assert len(seen) == 10


def test_tariff_guard_language(persian, monkeypatch):
    from app.cabinet.routes.subscription_modules import helpers
    monkeypatch.setattr(helpers, 'is_legacy_subscription', lambda subscription: True)
    with pytest.raises(HTTPException) as error:
        helpers.ensure_subscription_has_tariff(object(), language='fa')
    assert error.value.detail == {'code': 'tariff_required', 'message': persian.t('CABINET_SUBSCRIPTION_CHOOSE_TARIFF_FIRST')}


@pytest.mark.asyncio
async def test_subscription_owner_error(persian, monkeypatch):
    from app.cabinet.routes.subscription_modules import helpers
    from app.database.crud import subscription
    monkeypatch.setattr(type(helpers.settings), 'is_multi_tariff_enabled', lambda self: True)
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(subscription, 'get_subscription_by_id_for_user', lookup)
    db = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await helpers.resolve_subscription(db, SimpleNamespace(id=123, language='fa'), 456)
    assert error.value.status_code == 404 and error.value.detail == persian.t('CABINET_SUBSCRIPTION_NOT_FOUND')
    lookup.assert_awaited_once_with(db, 456, 123)
