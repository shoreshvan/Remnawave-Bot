"""Run shared authentication helpers with strict Persian messages."""

import ast
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, status

from app.database.models import UserStatus
from app.localization.texts import get_texts
from app.cabinet.utils.request_language import get_request_language
from tests.fixtures.persian import persian  # noqa: F401


@pytest.fixture
def auth_helpers():
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
             and node.name in {'_recover_cabinet_user_after_gate', '_require_legal_consent',
                              '_consume_widget_payload', '_consume_oidc_token',
                              'auth_telegram_widget', 'auth_telegram_oidc', 'auth_telegram'}]
    assert len(nodes) == 7
    for node in nodes:
        node.decorator_list = []
        node.args.defaults = []
    context = dict(get_texts=get_texts, HTTPException=HTTPException, status=status,
                   UserStatus=UserStatus, datetime=datetime, UTC=UTC, hashlib=hashlib,
                   get_request_language=get_request_language)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['blocked', 'active', 'deleted', 'admin'])
async def test_account_recovery(persian, monkeypatch, auth_helpers, case):
    from app.services import user_revival_service

    revive = AsyncMock()
    monkeypatch.setattr(user_revival_service, 'revive_deleted_user', revive)
    auth_helpers['is_env_admin_recovery'] = lambda user, decision: case == 'admin'
    original = UserStatus.ACTIVE.value if case == 'active' else (
        UserStatus.DELETED.value if case == 'deleted' else UserStatus.BLOCKED.value)
    user = SimpleNamespace(id=123, language='fa', status=original)
    db = AsyncMock()
    if case == 'blocked':
        with pytest.raises(HTTPException) as error:
            await auth_helpers['_recover_cabinet_user_after_gate'](db, user, None, source='test')
        assert error.value.status_code == 403
        assert error.value.detail == persian.t('CABINET_AUTH_ACCOUNT_NOT_ACTIVE')
        assert user.status == original
    else:
        await auth_helpers['_recover_cabinet_user_after_gate'](db, user, None, source='test')
    if case == 'deleted':
        revive.assert_awaited_once_with(db, user, source='test')
    else:
        revive.assert_not_awaited()
    if case == 'admin':
        assert user.status == UserStatus.ACTIVE.value
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing', 'accepted', 'disabled'])
async def test_legal_consent(persian, auth_helpers, case):
    documents = ['terms-v1', 'privacy-v2']
    requirement = SimpleNamespace(required=case != 'disabled', documents=documents, prechecked=False)
    service = SimpleNamespace(get_requirement=AsyncMock(return_value=requirement),
                              missing_documents=lambda documents, accepted: ['privacy-v2'] if case == 'missing' else [])
    auth_helpers['legal_consent_service'] = service
    db = AsyncMock()
    if case == 'missing':
        with pytest.raises(HTTPException) as error:
            await auth_helpers['_require_legal_consent'](db, accepted=['terms-v1'], language='fa')
        assert error.value.status_code == 428
        assert error.value.detail == dict(code='legal_consent_required',
                                         message=persian.t('CABINET_AUTH_LEGAL_CONSENT_REQUIRED'),
                                         documents=documents, missing=['privacy-v2'], prechecked=False)
    else:
        result = await auth_helpers['_require_legal_consent'](db, accepted=documents, language='fa')
        assert result == ([] if case == 'disabled' else documents)
    service.get_requirement.assert_awaited_once_with(db, 'fa')


@pytest.mark.asyncio
@pytest.mark.parametrize('replayed', [True, False])
@pytest.mark.parametrize('kind', ['widget', 'oidc'])
async def test_replay_guard(persian, auth_helpers, replayed, kind):
    cache = AsyncMock(return_value=replayed)
    auth_helpers['TokenReplayCache'] = SimpleNamespace(is_token_replayed=cache)
    if kind == 'widget':
        call = auth_helpers['_consume_widget_payload']({'hash': 'signed-hash'}, language='fa')
        hashed = hashlib.sha256(b'tg_widget:signed-hash').hexdigest()
        ttl, key = 86400, 'CABINET_AUTH_TELEGRAM_REPLAYED'
    else:
        call = auth_helpers['_consume_oidc_token']('signed-token', {'exp': 0}, language='fa')
        hashed = hashlib.sha256(b'signed-token').hexdigest()
        ttl, key = 60, 'CABINET_AUTH_OIDC_REPLAY_INVALID'
    if replayed:
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == 401 and error.value.detail == persian.t(key)
    else:
        assert await call is None
    cache.assert_awaited_once_with(hashed, ttl=ttl)


@pytest.mark.asyncio
@pytest.mark.parametrize(('kind', 'case', 'code', 'key'), [
    ('initdata', 'rate', 429, 'CABINET_LOGIN_RATE_LIMIT'),
    ('initdata', 'invalid', 401, 'CABINET_LOGIN_TELEGRAM_INVALID'),
    ('initdata', 'missing_id', 401, 'CABINET_LOGIN_OIDC_ID_MISSING'),
    ('widget', 'rate', 429, 'CABINET_LOGIN_RATE_LIMIT'),
    ('widget', 'invalid', 401, 'CABINET_LOGIN_TELEGRAM_INVALID'),
    ('oidc', 'rate', 429, 'CABINET_LOGIN_RATE_LIMIT'),
    ('oidc', 'disabled', 400, 'CABINET_LOGIN_OIDC_UNAVAILABLE'),
    ('oidc', 'invalid', 401, 'CABINET_AUTH_OIDC_REPLAY_INVALID'),
    ('oidc', 'bad_id', 401, 'CABINET_LOGIN_OIDC_ID_INVALID'),
    ('oidc', 'missing_id', 401, 'CABINET_LOGIN_OIDC_ID_MISSING'),
])
async def test_telegram_initial_errors(persian, auth_helpers, kind, case, code, key):
    rate = AsyncMock(return_value=case == 'rate')
    auth_helpers.update(
        get_client_ip=lambda request: '127.0.0.1',
        RateLimitCache=SimpleNamespace(is_ip_rate_limited=rate),
        _telegram_widget_signed_data=lambda request: {'hash': 'test'},
        validate_telegram_login_widget=lambda *args, **kwargs: False,
        validate_telegram_init_data=lambda *args, **kwargs: None if case == 'invalid' else {'first_name': 'نام'},
        get_setting_value=AsyncMock(return_value=None),
        settings=SimpleNamespace(TELEGRAM_OIDC_ENABLED=case != 'disabled', TELEGRAM_OIDC_CLIENT_ID='client'),
        validate_telegram_oidc_token=AsyncMock(return_value=(
            None if case == 'invalid' else {'sub': 'bad' if case == 'bad_id' else 0})),
    )
    request = SimpleNamespace(id_token='token', init_data='signed-data')
    raw = SimpleNamespace(headers={'Accept-Language': 'fa-IR,en;q=0.5'})
    with pytest.raises(HTTPException) as error:
        await auth_helpers['auth_telegram' if kind == 'initdata' else f'auth_telegram_{kind}'](request, raw, AsyncMock())
    assert error.value.status_code == code and error.value.detail == persian.t(key)
    rate.assert_awaited_once_with('127.0.0.1', f'telegram_{kind}', limit=10, window=60, fail_closed=True)
    if case == 'rate':
        assert error.value.headers == {'Retry-After': '60'}


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted', [False, True])
@pytest.mark.parametrize('kind', ['widget', 'initdata', 'oidc'])
@pytest.mark.parametrize('provider_language', [None, 'en'])
async def test_widget_registration_language(persian, monkeypatch, auth_helpers, accepted, kind, provider_language):
    from app.services import referral_service
    monkeypatch.setattr(referral_service, 'get_pending_referral', AsyncMock(return_value=None))
    requirement = SimpleNamespace(required=True, documents=['terms'], prechecked=False)
    replay = AsyncMock()
    class CreationReached(Exception):
        pass
    create = AsyncMock(side_effect=CreationReached)
    claims = {'id': 123, 'locale': provider_language}
    auth_helpers.update(
        logger=Mock(), get_client_ip=lambda request: '127.0.0.1',
        RateLimitCache=SimpleNamespace(is_ip_rate_limited=AsyncMock(return_value=False)),
        _telegram_widget_signed_data=lambda request: {'hash': 'test'},
        validate_telegram_login_widget=lambda *args, **kwargs: True,
        validate_telegram_init_data=lambda *args, **kwargs: {'id': 123, 'language_code': provider_language},
        validate_telegram_oidc_token=AsyncMock(return_value=claims),
        get_setting_value=AsyncMock(return_value=None),
        get_user_by_telegram_id=AsyncMock(return_value=None), _gate_cabinet_identity=AsyncMock(),
        RegistrationChannel=SimpleNamespace(CABINET_TELEGRAM_WIDGET='widget', CABINET_TELEGRAM_INIT_DATA='initdata',
                                             CABINET_TELEGRAM_OIDC='oidc'),
        settings=SimpleNamespace(is_admin=lambda user_id: False, TELEGRAM_OIDC_ENABLED=True, TELEGRAM_OIDC_CLIENT_ID='client'),
        legal_consent_service=SimpleNamespace(get_requirement=AsyncMock(return_value=requirement),
                                             missing_documents=lambda docs, values: [] if accepted else ['terms']),
        _consume_widget_payload=replay, _consume_oidc_token=replay, _create_or_claim_telegram_user=create,
    )
    request = SimpleNamespace(id=123, referral_code=None, accepted_legal_documents=['terms'] if accepted else [],
                               username='test', first_name='نام', last_name=None, init_data='signed', id_token='token')
    raw = SimpleNamespace(headers={'Accept-Language': 'fa-IR'})
    db = AsyncMock()
    with pytest.raises(CreationReached if accepted else HTTPException) as error:
        await auth_helpers['auth_telegram' if kind == 'initdata' else f'auth_telegram_{kind}'](request, raw, db)
    auth_helpers['legal_consent_service'].get_requirement.assert_awaited_once_with(db, 'fa')
    if accepted:
        if kind == 'widget':
            replay.assert_awaited_once_with({'hash': 'test'}, language='fa')
        elif kind == 'oidc':
            replay.assert_awaited_once_with('token', claims, language='fa')
        else:
            replay.assert_not_awaited()
        assert create.await_args.kwargs['language'] == ('fa' if kind == 'widget' else provider_language or 'fa')
        assert create.await_args.kwargs['telegram_id'] == 123
    else:
        assert error.value.status_code == 428
        assert error.value.detail['message'] == persian.t('CABINET_AUTH_LEGAL_CONSENT_REQUIRED')
        replay.assert_not_awaited()
        create.assert_not_awaited()
