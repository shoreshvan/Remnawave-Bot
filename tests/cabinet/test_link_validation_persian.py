from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.cabinet.routes import account_linking as routes
from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('path', 'body', 'key', 'limit'), [
    ('link/telegram', {}, 'CABINET_LINK_TELEGRAM_PROVIDE_ONE', None),
    ('link/telegram', {'init_data': 'data', 'id_token': 'token'}, 'CABINET_LINK_INPUT_MULTIPLE_METHODS', None),
    ('link/telegram', {'id': 123}, 'CABINET_LINK_INPUT_WIDGET_INCOMPLETE', None),
    ('link/telegram', {'id': 'invalid'}, 'CABINET_LINK_INPUT_INTEGER_REQUIRED', None),
    ('link/telegram', {'auth_date': 1.5}, 'CABINET_LINK_INPUT_INTEGER_REQUIRED', None),
    ('link/telegram', {'hash': 'short'}, 'CABINET_LINK_INPUT_HASH_INVALID', None),
    ('link/telegram', {'init_data': 123}, 'CABINET_LINK_INPUT_TEXT_REQUIRED', None),
    ('link/telegram', {'init_data': 'a' * 4097}, 'CABINET_LINK_INPUT_TOO_LONG', 4096),
    ('link/telegram', {'init_data': 'data', 'first_name': 'a' * 257}, 'CABINET_LINK_INPUT_TOO_LONG', 256),
    ('link/telegram', [], 'CABINET_INPUT_BODY_INVALID', None),
    ('link/google/callback', {}, 'CABINET_INPUT_REQUIRED', None),
    ('link/google/callback', {'code': '', 'state': 's'}, 'CABINET_LINK_INPUT_CODE_INVALID', None),
    ('link/google/callback', {'code': 'c', 'state': 's' * 129}, 'CABINET_LINK_INPUT_STATE_INVALID', None),
    ('link/google/callback', {'code': 'c', 'state': 's', 'device_id': 123}, 'CABINET_LINK_INPUT_TEXT_REQUIRED', None),
    ('link/unknown/callback', {'code': 'c', 'state': 's'}, 'CABINET_LINK_INPUT_PROVIDER_INVALID', None),
    ('unlink/unknown', {}, 'CABINET_LINK_INPUT_PROVIDER_INVALID', None),
    ('link/server-complete', {'code': 'c', 'state': 's', 'provider': 'unknown'}, 'CABINET_LINK_INPUT_PROVIDER_INVALID', None),
])
async def test_invalid_link_request(persian, monkeypatch, path, body, key, limit):
    rate, validate = AsyncMock(), AsyncMock()
    monkeypatch.setattr(routes.RateLimitCache, 'is_ip_rate_limited', rate)
    monkeypatch.setattr(routes, 'validate_oauth_state', validate)
    db = AsyncMock()
    app = FastAPI()
    app.include_router(routes.router, prefix='/cabinet')
    app.dependency_overrides[get_cabinet_db] = lambda: db
    app.dependency_overrides[get_current_cabinet_user] = lambda: SimpleNamespace(id=123, language='fa')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/cabinet/auth/account/' + path, json=body, headers={'Accept-Language': 'fa'})
    assert response.status_code == 422, response.text
    for error in response.json()['detail']:
        assert error['msg'] == persian.t(key).format(limit=limit)
        assert error['type'] and error['loc']
        if 'error' in error.get('ctx', {}):
            assert error['ctx']['error'] == error['msg']
    rate.assert_not_awaited()
    validate.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_english_validation_keeps_existing_behavior():
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_cabinet_db] = lambda: AsyncMock()
    app.dependency_overrides[get_current_cabinet_user] = lambda: SimpleNamespace(id=123, language='en')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/auth/account/link/google/callback', json={}, headers={'Accept-Language': 'en'})
    assert response.status_code == 422
    assert response.json()['detail'][0]['msg'] == 'Field required'
