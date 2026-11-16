from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.cabinet.routes import account_linking as routes
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['already', 'unavailable', 'success'])
async def test_init(persian, monkeypatch, case):
    provider = SimpleNamespace(prepare_auth_state=lambda: {'code_verifier': 'secret', '_challenge': 'challenge'},
                               get_authorization_url=Mock(return_value='https://example.test/authorize'))
    monkeypatch.setattr(routes, 'get_provider', Mock(return_value=None if case == 'unavailable' else provider))
    monkeypatch.setattr(routes, 'resolve_oauth_redirect_uri', lambda origin: 'https://example.test/callback')
    generate = AsyncMock(return_value='state')
    monkeypatch.setattr(routes, 'generate_oauth_state', generate)
    user = SimpleNamespace(id=123, language='fa', google_id='linked' if case == 'already' else None)
    if case == 'success':
        result = await routes.link_provider_init('google', SimpleNamespace(headers={}), user)
        assert result.state == 'state' and result.authorize_url == 'https://example.test/authorize'
        provider.get_authorization_url.assert_called_once_with('state', _challenge='challenge')
        assert generate.await_args.kwargs['extra_data']['code_verifier'] == 'secret'
        assert generate.await_args.kwargs['extra_data']['user_id'] == '123'
    else:
        with pytest.raises(HTTPException) as error:
            await routes.link_provider_init('google', SimpleNamespace(headers={}), user)
        key = 'CABINET_LINK_PROVIDER_ALREADY_LINKED' if case == 'already' else 'CABINET_LINK_OAUTH_PROVIDER_UNAVAILABLE'
        assert error.value.status_code == 400 and error.value.detail == persian.t(key)
        generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['expired', 'not_linking', 'wrong_user', 'success'])
async def test_callback(persian, monkeypatch, case):
    state = {'linking': 'false' if case == 'not_linking' else 'true', 'user_id': '456' if case == 'wrong_user' else '123'}
    monkeypatch.setattr(routes, 'validate_oauth_state', AsyncMock(return_value=None if case == 'expired' else state))
    exchange = AsyncMock(return_value=routes.LinkCallbackResponse(success=True, message='linked'))
    monkeypatch.setattr(routes, '_exchange_and_link_oauth', exchange)
    user, db = SimpleNamespace(id=123, language='fa'), AsyncMock()
    request = routes.LinkCallbackRequest(code='code', state='state')
    if case == 'success':
        assert (await routes.link_provider_callback('google', request, user, db)).success
        assert exchange.await_args.kwargs['user'] is user
        assert exchange.await_args.kwargs['state_data'] == state
    else:
        with pytest.raises(HTTPException) as error:
            await routes.link_provider_callback('google', request, user, db)
        key = {'expired': 'CABINET_LINK_OAUTH_STATE_INVALID', 'not_linking': 'CABINET_LINK_OAUTH_STATE_NOT_LINKING',
               'wrong_user': 'CABINET_LINK_OAUTH_STATE_WRONG_USER'}[case]
        assert error.value.status_code == 400 and error.value.detail == persian.t(key)
        exchange.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['limited', 'already', 'init_invalid', 'oidc_disabled', 'oidc_invalid',
    'oidc_replay', 'oidc_bad_id', 'oidc_no_id', 'widget_invalid', 'widget_replay',
    'merge', 'conflict', 'init_success', 'oidc_success', 'widget_success'])
async def test_telegram(persian, monkeypatch, case):
    from app.services import remnawave_resync_service
    user = SimpleNamespace(id=123, language='fa', telegram_id=456 if case == 'already' else None,
                           username='custom', first_name='نام', last_name=None)
    db = AsyncMock()
    if case == 'conflict':
        db.commit.side_effect = IntegrityError('sql', {}, Exception('private'))
    monkeypatch.setattr(routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=case == 'limited'))
    monkeypatch.setattr(routes, 'get_setting_value', AsyncMock(side_effect=['false' if case == 'oidc_disabled' else 'true', 'client']))
    monkeypatch.setattr(routes, 'validate_telegram_init_data', Mock(return_value=None if case == 'init_invalid' else
        {'id': 456, 'username': 'external', 'first_name': 'external', 'last_name': 'خانوادگی'}))
    claims = {'id': 'bad' if case == 'oidc_bad_id' else 0 if case == 'oidc_no_id' else 456, 'exp': 9999999999}
    monkeypatch.setattr(routes, 'validate_telegram_oidc_token', AsyncMock(return_value=None if case == 'oidc_invalid' else claims))
    replay = AsyncMock(return_value=case in {'widget_replay', 'oidc_replay'})
    monkeypatch.setattr(routes.TokenReplayCache, 'is_token_replayed', replay)
    monkeypatch.setattr(routes, 'validate_telegram_login_widget', lambda *args, **kwargs: case != 'widget_invalid')
    lookup = AsyncMock(return_value=SimpleNamespace(id=789) if case == 'merge' else None)
    monkeypatch.setattr(routes, 'get_user_by_telegram_id', lookup)
    merge = AsyncMock(return_value='merge-token')
    monkeypatch.setattr(routes, 'create_merge_token', merge)
    resync = AsyncMock(return_value={'synced': 1, 'failed': 0})
    monkeypatch.setattr(remnawave_resync_service, 'resync_user_subscriptions_with_panel', resync)
    request = (routes.LinkTelegramRequest(id_token='token') if case.startswith('oidc') else
        routes.LinkTelegramRequest(id=456, hash='a' * 64, auth_date=123) if case.startswith('widget') else
        routes.LinkTelegramRequest(init_data='data'))
    errors = {'limited': (429, 'CABINET_TOO_MANY_REQUESTS'), 'already': (400, 'CABINET_LINK_TELEGRAM_ALREADY_LINKED'),
        'init_invalid': (400, 'CABINET_LINK_TELEGRAM_INITDATA_INVALID'),
        'oidc_disabled': (400, 'CABINET_LINK_TELEGRAM_OIDC_NOT_CONFIGURED'),
        'oidc_invalid': (401, 'CABINET_LINK_TELEGRAM_OIDC_TOKEN_INVALID'),
        'oidc_replay': (401, 'CABINET_LINK_TELEGRAM_OIDC_TOKEN_INVALID'),
        'oidc_bad_id': (401, 'CABINET_LINK_TELEGRAM_OIDC_ID_INVALID'),
        'oidc_no_id': (401, 'CABINET_LINK_TELEGRAM_OIDC_ID_MISSING'),
        'widget_invalid': (400, 'CABINET_LINK_TELEGRAM_WIDGET_INVALID'),
        'widget_replay': (400, 'CABINET_LINK_TELEGRAM_WIDGET_REPLAYED'),
        'conflict': (409, 'CABINET_LINK_TELEGRAM_JUST_LINKED_OTHER')}
    call = routes.link_telegram(request, SimpleNamespace(headers={}, client=SimpleNamespace(host='127.0.0.1')), user, db)
    if case in errors:
        with pytest.raises(HTTPException) as error:
            await call
        status, key = errors[case]
        assert error.value.status_code == status and error.value.detail == persian.t(key)
        resync.assert_not_awaited()
        if case == 'conflict':
            db.rollback.assert_awaited_once()
        else:
            lookup.assert_not_awaited()
            db.commit.assert_not_awaited()
    else:
        result = await call
        if case == 'merge':
            assert result.merge_required and result.merge_token == 'merge-token'
            assert user.telegram_id is None
            db.commit.assert_not_awaited()
            merge.assert_awaited_once_with(primary_user_id=123, secondary_user_id=789, provider='telegram', provider_id='456')
        else:
            assert result.success and result.message == 'linked' and user.telegram_id == 456
            assert user.username == 'custom' and user.first_name == 'نام'
            db.commit.assert_awaited_once()
            resync.assert_awaited_once_with(db, user)
