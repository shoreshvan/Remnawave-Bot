from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.cabinet.routes import account_linking as routes
from app.cabinet.dependencies import get_cabinet_db
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('limited', 429, 'CABINET_TOO_MANY_REQUESTS'),
    ('expired', 400, 'CABINET_LINK_OAUTH_STATE_INVALID'),
    ('provider', 400, 'CABINET_LINK_PROVIDER_UNDETERMINED'),
    ('mismatch', 400, 'CABINET_LINK_PROVIDER_MISMATCH'),
    ('not_linking', 400, 'CABINET_LINK_OAUTH_STATE_NOT_LINKING'),
    ('missing_id', 400, 'CABINET_LINK_OAUTH_STATE_NOT_LINKING'),
    ('invalid_id', 400, 'CABINET_LINK_STATE_USER_INVALID'),
    ('no_user', 400, 'CABINET_AUTH_USER_NOT_FOUND'),
    ('success', 200, None), ('merge', 200, None),
])
async def test_server_completion(persian, monkeypatch, case, code, key):
    state = {'provider': 'unknown' if case == 'provider' else 'google',
             'linking': 'false' if case == 'not_linking' else 'true',
             'user_id': 'bad' if case == 'invalid_id' else '' if case == 'missing_id' else '123'}
    validate = AsyncMock(return_value=None if case == 'expired' else state)
    monkeypatch.setattr(routes, 'validate_oauth_state', validate)
    monkeypatch.setattr(routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=case == 'limited'))
    user = SimpleNamespace(id=123, language='fa')
    lookup = AsyncMock(return_value=None if case == 'no_user' else user)
    monkeypatch.setattr(routes, 'get_user_by_id', lookup)
    result = routes.LinkCallbackResponse(success=case != 'merge', message='linked' if case != 'merge' else None,
        merge_required=case == 'merge', merge_token='merge-token' if case == 'merge' else None)
    exchange = AsyncMock(return_value=result)
    monkeypatch.setattr(routes, '_exchange_and_link_oauth', exchange)
    db = AsyncMock()
    app = FastAPI()
    app.include_router(routes.router, prefix='/cabinet')
    app.dependency_overrides[get_cabinet_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/cabinet/auth/account/link/server-complete',
            json={'code': 'auth-code', 'state': 'state-token', 'provider': 'discord' if case == 'mismatch' else None},
            headers={'Accept-Language': 'fa'})
    assert response.status_code == code, response.text
    if key:
        assert response.json()['detail'] == persian.t(key)
        exchange.assert_not_awaited()
    else:
        assert response.json() == {**result.model_dump(), 'provider': 'google'}
        exchange.assert_awaited_once_with(db=db, user=user, provider='google', code='auth-code',
            state='state-token', state_data=state, device_id=None, log_context='server-complete')
    db.commit.assert_not_awaited()
    if case == 'limited':
        validate.assert_not_awaited()
    if case not in {'no_user', 'success', 'merge'}:
        lookup.assert_not_awaited()
