from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.cabinet.routes import account_linking as routes
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'status', 'key'), [
    ('unavailable', 400, 'CABINET_LINK_OAUTH_PROVIDER_UNAVAILABLE'),
    ('exchange', 400, 'CABINET_LINK_OAUTH_EXCHANGE_FAILED'),
    ('userinfo', 400, 'CABINET_LINK_OAUTH_USERINFO_FAILED'),
    ('occupied', 409, 'CABINET_LINK_OAUTH_PROVIDER_SLOT_OCCUPIED'),
    ('conflict', 409, 'CABINET_LINK_OAUTH_JUST_LINKED_OTHER'),
    ('already', None, None), ('merge', None, None), ('success', None, None),
])
async def test_exchange(persian, monkeypatch, case, status, key):
    user = SimpleNamespace(id=123, language='fa', email='owner@example.com',
        google_id='other' if case == 'occupied' else 'provider-id' if case == 'already' else None)
    info = SimpleNamespace(provider_id='provider-id', email='owner@example.com', email_verified=True)
    provider = SimpleNamespace(exchange_code=AsyncMock(return_value={'access_token': 'secret'},
        side_effect=RuntimeError('private provider diagnostic') if case == 'exchange' else None),
        get_user_info=AsyncMock(return_value=info,
        side_effect=RuntimeError('private provider diagnostic') if case == 'userinfo' else None))
    monkeypatch.setattr(routes, 'get_provider', Mock(return_value=None if case == 'unavailable' else provider))
    monkeypatch.setattr(routes, 'get_user_by_oauth_provider', AsyncMock(return_value=SimpleNamespace(id=456) if case == 'merge' else None))
    merge, link = AsyncMock(return_value='merge-token'), AsyncMock()
    monkeypatch.setattr(routes, 'create_merge_token', merge)
    monkeypatch.setattr(routes, 'set_user_oauth_provider_id', link)
    db = AsyncMock()
    if case == 'conflict':
        db.commit.side_effect = IntegrityError('sql', {}, Exception('private'))
    call = routes._exchange_and_link_oauth(db=db, user=user, provider='google', code='code', state='state',
        state_data={'oauth_redirect_uri': 'https://example.test/callback', 'code_verifier': 'verifier'},
        device_id='device', log_context='test')
    if status:
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == status
        assert error.value.detail == persian.t(key).format(provider='google')
        assert 'private' not in error.value.detail
    else:
        result = await call
        if case == 'merge':
            assert result.merge_required and result.merge_token == 'merge-token'
            merge.assert_awaited_once_with(primary_user_id=123, secondary_user_id=456,
                                           provider='google', provider_id='provider-id')
        else:
            assert result.success and result.message == ('already_linked' if case == 'already' else 'linked')
    if case not in {'unavailable'}:
        provider.exchange_code.assert_awaited_once_with('code', state='state', code_verifier='verifier', device_id='device')
    if case not in {'success', 'conflict'}:
        link.assert_not_awaited()
        db.commit.assert_not_awaited()
    if case == 'conflict':
        db.rollback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['not_linked', 'last', 'success'])
async def test_unlink(persian, monkeypatch, case):
    user = SimpleNamespace(id=123, language='fa', google_id=None if case == 'not_linked' else 'google-id',
        telegram_id=456 if case == 'success' else None, email=None, password_hash=None,
        yandex_id=None, discord_id=None, vk_id=None)
    clear = AsyncMock()
    monkeypatch.setattr(routes, 'clear_user_oauth_provider_id', clear)
    db = AsyncMock()
    if case == 'success':
        assert (await routes.unlink_provider('google', user, db)).success
        clear.assert_awaited_once_with(db, user, 'google')
        db.commit.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as error:
            await routes.unlink_provider('google', user, db)
        key = 'CABINET_LINK_PROVIDER_NOT_LINKED' if case == 'not_linked' else 'CABINET_LINK_CANNOT_UNLINK_LAST'
        assert error.value.status_code == 400 and error.value.detail == persian.t(key)
        clear.assert_not_awaited()
        db.commit.assert_not_awaited()
