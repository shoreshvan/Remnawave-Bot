import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

from app.database.crud import user as crud
from app.localization.texts import get_texts
from app.cabinet.utils.request_language import get_request_language
from tests.fixtures.persian import persian  # noqa: F401


@pytest.fixture
def routes():
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
             and node.name in {'request_email_change', 'verify_email_change', 'cancel_email_change', 'get_email_change_status'}]
    for node in nodes:
        node.decorator_list = []
        node.args.defaults = []
    context = dict(get_texts=get_texts, HTTPException=HTTPException, status=status,
                   get_client_ip=lambda request: '127.0.0.1',
                   RateLimitCache=SimpleNamespace(is_ip_rate_limited=AsyncMock(return_value=False)),
                   verify_and_apply_email_change=crud.verify_and_apply_email_change,
                   clear_email_change_pending=crud.clear_email_change_pending)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['ip', 'account', 'missing', 'same', 'admin', 'disposable', 'taken',
    'conflict', 'unconfigured', 'sent', 'failed', 'replace', 'replace_failed', 'replace_exception',
    'replace_disabled', 'replace_unconfigured', 'missing_language'])
async def test_request_change(persian, routes, case):
    user, db = account(), AsyncMock()
    user.first_name = 'نام'
    user.email_verified = not (case.startswith('replace') or case == 'conflict')
    if case == 'missing_language':
        user.language = None
    if case == 'missing':
        user.email = None
    if case == 'conflict':
        db.commit.side_effect = IntegrityError('statement', {}, Exception('private'))
    rate = routes['RateLimitCache'].is_ip_rate_limited
    rate.side_effect = [True] if case == 'ip' else [False, case == 'account']
    send = AsyncMock(return_value=case not in {'failed', 'replace_failed'})
    if case == 'replace_exception':
        send.side_effect = RuntimeError('private diagnostic')
    pending, clear = AsyncMock(), AsyncMock()
    override = AsyncMock(return_value=('عنوان مدیر', '<p>متن مدیر</p>'))
    routes.update(get_request_language=get_request_language, IntegrityError=IntegrityError,
        EmailChangeResponse=SimpleNamespace, logger=Mock(),
        is_email_taken=AsyncMock(return_value=case == 'taken'),
        disposable_email_service=SimpleNamespace(is_disposable=lambda email: case == 'disposable'),
        settings=SimpleNamespace(get_admin_emails=lambda: ['new@example.com'] if case == 'admin' else [],
            is_cabinet_email_verification_enabled=lambda: case != 'replace_disabled',
            get_cabinet_email_verification_expire_hours=lambda: 2,
            get_cabinet_email_change_code_expire_minutes=lambda: 10, CABINET_URL='https://example.test'),
        email_service=SimpleNamespace(is_configured=lambda: case not in {'unconfigured', 'replace_unconfigured'},
            send_verification_email=Mock(), send_email_change_code=Mock()),
        get_rendered_override=override, asyncio=SimpleNamespace(to_thread=send),
        generate_verification_token=lambda: 'token', get_verification_expires_at=lambda: 'expiry',
        generate_email_change_code=lambda: '012345', get_email_change_expires_at=lambda: 'expiry',
        set_email_change_pending=pending, clear_email_change_pending=clear)
    errors = {'ip': (429, 'CABINET_TOO_MANY_REQUESTS'), 'account': (429, 'CABINET_TOO_MANY_REQUESTS'),
        'missing': (400, 'CABINET_AUTH_NO_EMAIL_TO_CHANGE'), 'same': (400, 'CABINET_AUTH_NEW_EMAIL_SAME_AS_CURRENT'),
        'admin': (403, 'CABINET_AUTH_EMAIL_CANNOT_BE_LINKED'),
        'disposable': (400, 'CABINET_AUTH_DISPOSABLE_EMAIL_NOT_ALLOWED'),
        'taken': (400, 'CABINET_AUTH_EMAIL_ALREADY_REGISTERED'),
        'conflict': (400, 'CABINET_AUTH_EMAIL_ALREADY_REGISTERED'),
        'unconfigured': (503, 'CABINET_AUTH_EMAIL_SERVICE_NOT_CONFIGURED')}
    call = routes['request_email_change'](SimpleNamespace(new_email='OLD@example.com' if case == 'same' else 'new@example.com'),
        SimpleNamespace(headers={'Accept-Language': 'fa'}), user, db)
    if case in errors:
        with pytest.raises(HTTPException) as error:
            await call
        code, key = errors[case]
        assert error.value.status_code == code and error.value.detail == persian.t(key)
        send.assert_not_awaited()
        if case == 'conflict':
            db.rollback.assert_awaited_once()
        elif case == 'unconfigured':
            clear.assert_awaited_once_with(db, user)
        else:
            pending.assert_not_awaited()
            db.commit.assert_not_awaited()
        return
    result = await call
    key = ('CABINET_EMAIL_REPLACED' if case == 'replace_disabled' else
           'CABINET_EMAIL_REPLACED_NOT_SENT' if case in {'replace_failed', 'replace_exception', 'replace_unconfigured'} else
           'CABINET_AUTH_EMAIL_REPLACED_VERIFICATION_SENT' if case == 'replace' else
           'CABINET_AUTH_MERGE_CODE_NOT_SENT' if case == 'failed' else 'CABINET_AUTH_EMAIL_CHANGE_CODE_SENT')
    assert result.message == persian.t(key) and result.new_email == 'new@example.com'
    replacing = case.startswith('replace')
    assert result.expires_in_minutes == (0 if replacing else 10)
    if replacing:
        assert user.email == 'new@example.com' and not user.email_verified
        assert user.email_verification_token == 'token'
        db.commit.assert_awaited_once()
        pending.assert_not_awaited()
    else:
        assert user.email == 'old@example.com' and user.email_verified
        pending.assert_awaited_once_with(db, user, 'new@example.com', '012345', 'expiry')
        db.commit.assert_not_awaited()
    if case in {'replace_disabled', 'replace_unconfigured'}:
        send.assert_not_awaited()
    else:
        assert send.await_args.kwargs['language'] == 'fa'
        assert send.await_args.kwargs['custom_subject'] == 'عنوان مدیر'
        assert send.await_args.kwargs['custom_body_html'] == '<p>متن مدیر</p>'
        assert override.await_args.args[1] == 'fa'


def account():
    return SimpleNamespace(id=123, language='fa', email='old@example.com', email_verified=False,
        email_change_new='new@example.com', email_change_code='012345',
        email_change_expires=datetime.now(UTC) + timedelta(minutes=10))


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'key'), [
    ('missing', 'CABINET_AUTH_NO_PENDING_EMAIL_CHANGE'),
    ('expired', 'CABINET_EMAIL_CHANGE_CODE_EXPIRED'),
    ('wrong', 'CABINET_AUTH_INVALID_CONFIRMATION_CODE'),
    ('taken', 'CABINET_AUTH_EMAIL_ALREADY_REGISTERED'),
    ('success', 'CABINET_EMAIL_CHANGE_SUCCESS'),
])
async def test_confirmation_with_real_helper(persian, monkeypatch, routes, case, key):
    user, db = account(), AsyncMock()
    if case == 'missing':
        user.email_change_new = None
    if case == 'expired':
        user.email_change_expires = datetime.now(UTC) - timedelta(seconds=1)
    lookup = AsyncMock(return_value=SimpleNamespace(id=456) if case == 'taken' else None)
    monkeypatch.setattr(crud, 'get_user_by_email', lookup)
    request = SimpleNamespace(code='999999' if case == 'wrong' else '012345')
    if case == 'success':
        result = await routes['verify_email_change'](request, object(), user, db)
        assert result == {'message': persian.t(key), 'new_email': 'new@example.com'}
        assert user.email_verified and user.email_verification_source == 'cabinet'
        db.refresh.assert_awaited_once_with(user)
    else:
        with pytest.raises(HTTPException) as error:
            await routes['verify_email_change'](request, object(), user, db)
        assert error.value.status_code == 400 and error.value.detail == persian.t(key)
        assert user.email == 'old@example.com' and not user.email_verified
        db.refresh.assert_not_awaited()
    if case in {'expired', 'taken', 'success'}:
        assert user.email_change_new is None and user.email_change_code is None
        assert user.email_change_expires is None
        db.commit.assert_awaited_once()
    else:
        db.commit.assert_not_awaited()
    if case in {'missing', 'expired', 'wrong'}:
        lookup.assert_not_awaited()
    if case == 'wrong':
        assert user.email_change_code == '012345'


@pytest.mark.asyncio
@pytest.mark.parametrize('limited_by', ['ip', 'account'])
async def test_confirmation_limits(persian, routes, limited_by):
    user, db = account(), AsyncMock()
    verify = AsyncMock()
    routes['verify_and_apply_email_change'] = verify
    routes['RateLimitCache'].is_ip_rate_limited.side_effect = [True] if limited_by == 'ip' else [False, True]
    with pytest.raises(HTTPException) as error:
        await routes['verify_email_change'](SimpleNamespace(code='012345'), object(), user, db)
    key = 'CABINET_TOO_MANY_REQUESTS' if limited_by == 'ip' else 'CABINET_AUTH_CHANGE_TOO_MANY_ATTEMPTS'
    assert error.value.status_code == 429 and error.value.detail == persian.t(key)
    verify.assert_not_awaited()
    if limited_by == 'account':
        assert user.email_change_code is None and user.email_change_new is None
        db.commit.assert_awaited_once()
    else:
        assert error.value.headers == {'Retry-After': '60'}
        assert user.email_change_code == '012345'
        db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('pending', [False, True])
async def test_cancel_and_status(persian, routes, pending):
    user, db = account(), AsyncMock()
    if not pending:
        user.email_change_new = None
    before = await routes['get_email_change_status'](user)
    assert before['pending'] is pending
    if pending:
        assert before['new_email'] == 'new@example.com'
        assert before['expires_at'] == user.email_change_expires.isoformat()
        result = await routes['cancel_email_change'](user, db)
        assert result == {'message': persian.t('CABINET_AUTH_EMAIL_CHANGE_CANCELLED')}
        db.commit.assert_awaited_once()
        assert user.email == 'old@example.com'
    else:
        with pytest.raises(HTTPException) as error:
            await routes['cancel_email_change'](user, db)
        assert error.value.status_code == 400
        assert error.value.detail == persian.t('CABINET_AUTH_NO_PENDING_EMAIL_CHANGE')
        db.commit.assert_not_awaited()
    assert await routes['get_email_change_status'](user) == {'pending': False, 'new_email': None, 'expires_at': None}
