from types import SimpleNamespace
import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.cabinet.routes import auth
from app.cabinet.dependencies import get_cabinet_db
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('limited', 429, 'CABINET_TOO_MANY_REQUESTS'),
    ('unavailable', 503, 'CABINET_DEEPLINK_UNAVAILABLE'),
    ('no_bot', 503, 'CABINET_DEEPLINK_BOT_UNCONFIGURED'),
    ('success', 200, None),
])
async def test_request_http(persian, monkeypatch, case, code, key):
    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=case == 'limited'))
    create = AsyncMock(return_value='a' * 32, side_effect=RuntimeError('private') if case == 'unavailable' else None)
    monkeypatch.setattr(auth, 'create_web_auth_token', create)
    monkeypatch.setattr(type(auth.settings), 'get_bot_username', lambda self: None if case == 'no_bot' else 'test_bot')
    app = FastAPI()
    app.include_router(auth.router, prefix='/cabinet')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/cabinet/auth/deeplink/request', headers={'Accept-Language': 'fa'})
    assert response.status_code == code
    if key:
        assert response.json()['detail'] == persian.t(key)
    else:
        assert response.json() == {'token': 'a' * 32, 'bot_username': 'test_bot', 'expires_in': auth.WEB_AUTH_TOKEN_TTL}
    if case == 'limited':
        create.assert_not_awaited()
        assert response.headers['retry-after'] == '60'


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('limited', 429, 'CABINET_TOO_MANY_REQUESTS'),
    ('expired', 410, 'CABINET_DEEPLINK_EXPIRED'),
    ('pending', 202, 'CABINET_DEEPLINK_PENDING'),
    ('invalid', 410, 'CABINET_DEEPLINK_INVALID_STATE'),
    ('consumed', 410, 'CABINET_DEEPLINK_CONSUMED'),
    ('bad_data', 500, 'CABINET_DEEPLINK_INVALID_DATA'),
    ('no_user', 401, 'CABINET_AUTH_USER_NOT_FOUND'),
    ('inactive', 403, 'CABINET_DEEPLINK_INACTIVE'),
])
async def test_poll_http(persian, monkeypatch, case, code, key):
    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=case == 'limited'))
    poll = AsyncMock(return_value=None if case == 'expired' else
        {'status': case if case in {'pending', 'invalid'} else 'linked'})
    consume = AsyncMock(return_value=None if case == 'consumed' else
        {'status': 'linked'} if case == 'bad_data' else {'user_id': 123})
    monkeypatch.setattr(auth, 'poll_web_auth_token', poll)
    monkeypatch.setattr(auth, 'consume_web_auth_token', consume)
    monkeypatch.setattr(auth, 'get_user_by_id', AsyncMock(return_value=None if case == 'no_user' else
        SimpleNamespace(id=123, status='disabled', language='fa')))
    create = AsyncMock()
    monkeypatch.setattr(auth, '_create_auth_response', create)
    db = AsyncMock()
    app = FastAPI()
    app.include_router(auth.router, prefix='/cabinet')
    app.dependency_overrides[get_cabinet_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/cabinet/auth/deeplink/poll', json={'token': 'a' * 32}, headers={'Accept-Language': 'fa'})
    assert response.status_code == code, response.text
    assert response.json()['detail'] == persian.t(key)
    db.commit.assert_not_awaited()
    create.assert_not_awaited()
    if case in {'limited', 'expired', 'pending', 'invalid'}:
        consume.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_preserves_tokens(persian, monkeypatch):
    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(auth, 'poll_web_auth_token', AsyncMock(return_value={'status': 'linked'}))
    consume = AsyncMock(return_value={'user_id': 123})
    monkeypatch.setattr(auth, 'consume_web_auth_token', consume)
    user = SimpleNamespace(id=123, telegram_id=456, language='fa', status='active')
    monkeypatch.setattr(auth, 'get_user_by_id', AsyncMock(return_value=user))
    response = SimpleNamespace(refresh_token='refresh', access_token='access', campaign_bonus=None)
    monkeypatch.setattr(auth, '_create_auth_response', AsyncMock(return_value=response))
    store = AsyncMock()
    monkeypatch.setattr(auth, '_store_refresh_token', store)
    monkeypatch.setattr(auth, '_process_campaign_bonus', AsyncMock(return_value=None))
    db = AsyncMock()
    result = await auth.poll_deep_link_token(SimpleNamespace(token='a' * 32, campaign_slug=None),
        SimpleNamespace(headers={'Accept-Language': 'fa'}, client=SimpleNamespace(host='127.0.0.1')), db)
    assert result is response and result.access_token == 'access'
    consume.assert_awaited_once_with('a' * 32)
    store.assert_awaited_once_with(db, 123, 'refresh', device_info='deep_link')
    db.commit.assert_awaited_once()


def test_missing_peer_uses_interface_language(persian):
    from fastapi import HTTPException
    from app.cabinet.ip_utils import get_client_ip

    with pytest.raises(HTTPException) as error:
        get_client_ip(SimpleNamespace(headers={'Accept-Language': 'fa'}, client=None))
    assert error.value.status_code == 400
    assert error.value.detail == persian.t('CABINET_CLIENT_IP_UNDETERMINED')


@pytest.mark.asyncio
@pytest.mark.parametrize(('body', 'key'), [
    ({}, 'CABINET_INPUT_REQUIRED'),
    ({'token': 'short'}, 'CABINET_INPUT_DEEPLINK_TOKEN_INVALID'),
    ({'token': 'a' * 129}, 'CABINET_INPUT_DEEPLINK_TOKEN_INVALID'),
    ({'token': 123}, 'CABINET_INPUT_DEEPLINK_TOKEN_INVALID'),
    ({'token': 'a' * 32, 'campaign_slug': 'bad value'}, 'CABINET_INPUT_CAMPAIGN_INVALID'),
    ({'token': 'a' * 32, 'campaign_slug': ''}, 'CABINET_INPUT_CAMPAIGN_INVALID'),
])
async def test_validation(persian, body, key):
    app = FastAPI()
    app.include_router(auth.router, prefix='/cabinet')
    app.dependency_overrides[get_cabinet_db] = lambda: AsyncMock()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/cabinet/auth/deeplink/poll', json=body, headers={'Accept-Language': 'fa'})
    assert response.status_code == 422
    assert response.json()['detail'][0]['msg'] == persian.t(key)


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'key'), [
    ('deny', 'WEB_AUTH_CANCELLED'), ('invalid', 'WEB_AUTH_INVALID_TOKEN'),
    ('missing', 'WEB_AUTH_INACTIVE'), ('inactive', 'WEB_AUTH_INACTIVE'),
    ('success', 'WEB_AUTH_SUCCESS'), ('expired', 'WEB_AUTH_EXPIRED'),
])
async def test_bot_confirmation(persian, case, key):
    path = Path(__file__).resolve().parents[2] / 'app/handlers/start.py'
    node = next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == 'process_webauth_confirm')
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    class Message:
        edit_text = AsyncMock()
    user = SimpleNamespace(id=123, language='fa', status='disabled' if case == 'inactive' else 'active')
    linked = AsyncMock(return_value=case == 'success')
    context = dict(types=SimpleNamespace(Message=Message), get_texts=auth.get_texts,
        UserStatus=auth.UserStatus, WEB_AUTH_TOKEN_MIN_LENGTH=16,
        get_user_by_telegram_id=AsyncMock(return_value=None if case == 'missing' else user),
        link_web_auth_token=linked)
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    data = 'webauth_deny' if case == 'deny' else 'webauth_confirm:' + ('short' if case == 'invalid' else 'a' * 32)
    callback = SimpleNamespace(data=data, message=Message(), answer=AsyncMock(),
                               from_user=SimpleNamespace(id=456, language_code='en'))
    await context['process_webauth_confirm'](callback, AsyncMock(), user)
    callback.message.edit_text.assert_awaited_once_with(persian.t(key))
    if case in {'success', 'expired'}:
        linked.assert_awaited_once_with('a' * 32, 456, 123)
    else:
        linked.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['balance', 'subscription', None])
async def test_campaign_payload_preserves_data(persian, monkeypatch, kind):
    from unittest.mock import Mock
    result = SimpleNamespace(campaign_name='نام سفارشی', bonus_type=kind, balance_kopeks=125000,
                             subscription_days=7, tariff_name='طرح سفارشی')
    service = SimpleNamespace(attribute_campaign=AsyncMock(return_value=result))
    monkeypatch.setattr(auth, 'AdvertisingCampaignService', Mock(return_value=service))
    db, user = AsyncMock(), SimpleNamespace(id=123, language='fa')
    bonus = await auth._process_campaign_bonus(db, user, 'campaign-1')
    assert bonus.campaign_name == 'نام سفارشی' and bonus.tariff_name == 'طرح سفارشی'
    assert bonus.bonus_type == (kind or 'none')
    assert bonus.balance_kopeks == 125000 and bonus.subscription_days == 7
    service.attribute_campaign.assert_awaited_once_with(db, user, 'campaign-1')


def test_deeplink_schema_metadata():
    from app.cabinet.schemas.auth import DeepLinkPollRequest, DeepLinkTokenResponse
    for model in (DeepLinkPollRequest, DeepLinkTokenResponse):
        schema = model.model_json_schema()
        for entry in [schema, *schema['properties'].values()]:
            assert any('\u0600' <= c <= '\u06ff' for c in entry['title'])
            assert any('\u0600' <= c <= '\u06ff' for c in entry['description'])
