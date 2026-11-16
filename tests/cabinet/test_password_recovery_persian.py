import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, status
from sqlalchemy import select, func

from app.database.models import User
from app.localization.texts import get_texts
from app.cabinet.utils.request_language import get_request_language
from tests.fixtures.persian import persian  # noqa: F401


@pytest.fixture
def recovery():
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
             and node.name in {'forgot_password', 'reset_password'}]
    assert len(nodes) == 2
    for node in nodes:
        node.decorator_list = []
        node.args.defaults = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             *nodes], type_ignores=[])
    context = dict(get_texts=get_texts, get_request_language=get_request_language, HTTPException=HTTPException,
                   status=status, select=select, func=func, User=User, datetime=datetime, UTC=UTC,
                   require_email_auth_enabled=AsyncMock(), get_client_ip=lambda request: '127.0.0.1',
                   RateLimitCache=SimpleNamespace(is_ip_rate_limited=AsyncMock(return_value=False)))
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    return context


def inputs(user):
    return (SimpleNamespace(email='test@example.com', token='token', password='new-password'),
            SimpleNamespace(headers={'Accept-Language': 'fa'}),
            SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: user)),
                            commit=AsyncMock()))


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing', 'unverified', 'verified', 'guest'])
async def test_forgot_same_response(persian, recovery, case):
    user = None if case == 'missing' else SimpleNamespace(email_verified=case == 'verified',
        password_hash='hash' if case == 'guest' else None, auth_type='email', password_reset_token=None)
    generate = Mock(return_value='new-token')
    recovery.update(generate_password_reset_token=generate, get_password_reset_expires_at=lambda: 'expiry',
                    email_service=SimpleNamespace(is_configured=lambda: False))
    request, raw, db = inputs(user)
    result = await recovery['forgot_password'](request, raw, db)
    assert result == {'message': persian.t('CABINET_PASSWORD_RESET_REQUESTED')}
    recovery['require_email_auth_enabled'].assert_awaited_once_with(db, language='fa')
    if case in {'missing', 'unverified'}:
        generate.assert_not_called()
        db.commit.assert_not_awaited()
    else:
        assert user.password_reset_token == 'new-token' and user.password_reset_expires == 'expiry'
        assert db.commit.await_count == (2 if case == 'guest' else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing', 'expired', 'success'])
async def test_reset(persian, recovery, case):
    user = None if case == 'missing' else SimpleNamespace(password_hash='old-hash',
                                                         password_reset_token='token', password_reset_expires='expiry')
    hashing = Mock(return_value='new-hash')
    recovery.update(is_token_expired=lambda value: case == 'expired', hash_password=hashing)
    request, raw, db = inputs(user)
    if case == 'success':
        assert await recovery['reset_password'](request, raw, db) == {'message': persian.t('CABINET_AUTH_PASSWORD_RESET_SUCCESS')}
        assert user.password_hash == 'new-hash'
        assert user.password_reset_token is None and user.password_reset_expires is None
        hashing.assert_called_once_with('new-password')
        db.commit.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as error:
            await recovery['reset_password'](request, raw, db)
        assert error.value.status_code == 400
        assert error.value.detail == persian.t('CABINET_PASSWORD_RESET_TOKEN_INVALID' if case == 'missing'
                                              else 'CABINET_AUTH_RESET_TOKEN_EXPIRED')
        hashing.assert_not_called()
        db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(('name', 'limit'), [('forgot_password', 3), ('reset_password', 5)])
async def test_limits(persian, recovery, name, limit):
    rate = recovery['RateLimitCache'].is_ip_rate_limited
    rate.return_value = True
    request, raw, db = inputs(None)
    with pytest.raises(HTTPException) as error:
        await recovery[name](request, raw, db)
    assert error.value.status_code == 429 and error.value.headers == {'Retry-After': '60'}
    assert error.value.detail == persian.t('CABINET_LOGIN_RATE_LIMIT')
    rate.assert_awaited_once_with('127.0.0.1', 'password_forgot' if name == 'forgot_password' else 'password_reset',
                                 limit=limit, window=60, fail_closed=True)
    db.execute.assert_not_awaited()


@pytest.mark.parametrize('hours', [1, 24])
@pytest.mark.parametrize('username', [None, 'نام <کاربر>'])
def test_real_reset_email(persian, monkeypatch, hours, username):
    from app.cabinet.services.email_service import EmailService
    from app.config import settings

    monkeypatch.setattr(type(settings), 'get_cabinet_password_reset_expire_hours', lambda self: hours)
    service = EmailService()
    send = Mock(return_value=True)
    monkeypatch.setattr(service, 'send_email', send)
    assert service.send_password_reset_email('test@example.com', 'token-123', 'https://example.test/reset',
                                             username=username, language='fa') is True
    recipient, subject, body = send.call_args.args
    assert recipient == 'test@example.com' and 'بازنشانی رمز عبور' in subject
    assert '<html lang="fa" dir="rtl">' in body
    assert f'این لینک تا {hours} ساعت معتبر است.' in body
    assert 'https://example.test/reset?token=token-123' in body
    assert '<bdi dir="ltr">' in body
    assert 'این ایمیل را نادیده بگیرید' in body
    if username:
        assert 'نام &lt;کاربر&gt;' in body and 'نام <کاربر>' not in body
    assert not any('\u0400' <= char <= '\u04ff' for char in subject + body)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['fa', None])
@pytest.mark.parametrize('custom', [False, True])
async def test_reset_email_language_and_custom_template(persian, recovery, language, custom):
    user = SimpleNamespace(email_verified=True, language=language, email='test@example.com', first_name='نام')
    send = Mock()
    thread = AsyncMock(return_value=True)
    override = ('عنوان سفارشی', '<p>متن مدیر</p>') if custom else None
    render = AsyncMock(return_value=override)
    recovery.update(generate_password_reset_token=lambda: 'token', get_password_reset_expires_at=lambda: 'expiry',
                    email_service=SimpleNamespace(is_configured=lambda: True, send_password_reset_email=send),
                    settings=SimpleNamespace(CABINET_URL='https://example.test',
                                              get_cabinet_password_reset_expire_hours=lambda: 2),
                    get_rendered_override=render, asyncio=SimpleNamespace(to_thread=thread))
    request, raw, db = inputs(user)
    assert await recovery['forgot_password'](request, raw, db) == {'message': persian.t('CABINET_PASSWORD_RESET_REQUESTED')}
    assert render.await_args.args == ('password_reset', 'fa')
    assert render.await_args.kwargs['context']['reset_url'] == 'https://example.test/reset-password?token=token'
    thread.assert_awaited_once_with(send, to_email=user.email, reset_token='token',
        reset_url='https://example.test/reset-password', username='نام', language='fa',
        custom_subject=override[0] if custom else None, custom_body_html=override[1] if custom else None)
