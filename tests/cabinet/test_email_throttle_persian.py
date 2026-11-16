"""Persian throttle errors preserve windows, hashed subjects and fail-closed behavior."""

import ast
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app.database.models import User
from app.localization.texts import get_texts

from app.cabinet.auth import registration_throttle as throttle
from app.cabinet.utils.request_language import get_request_language
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('window_index', [0, 1, 2])
@pytest.mark.parametrize('kind', ['register', 'resend'])
async def test_throttled_persian_message(persian, monkeypatch, kind, window_index):
    windows = [('minute', 5, 60), ('hour', 10, 3600), ('day', 30, 86400)]
    monkeypatch.setattr(throttle, '_windows', lambda: tuple(windows))
    monkeypatch.setattr(throttle, '_resend_ip_windows', lambda: tuple(windows[:2]))
    monkeypatch.setattr(throttle.settings, 'CABINET_EMAIL_RESEND_PER_ADDRESS_PER_HOUR', 4)
    ip_check = AsyncMock(side_effect=[False] * window_index + [True])
    subject_check = AsyncMock(return_value=True)
    monkeypatch.setattr(throttle.RateLimitCache, 'is_ip_rate_limited', ip_check)
    monkeypatch.setattr(throttle.RateLimitCache, 'is_subject_rate_limited', subject_check)
    with pytest.raises(HTTPException) as error:
        if kind == 'register':
            await throttle.enforce_email_registration_throttle('127.0.0.1', language='fa')
        else:
            await throttle.enforce_verification_resend_throttle('127.0.0.1', ' USER@Example.com ', language='fa')
    assert error.value.status_code == 429
    assert error.value.detail == persian.t('CABINET_LOGIN_RATE_LIMIT')
    retry = 3600 if kind == 'resend' and window_index == 2 else windows[window_index][2]
    assert error.value.headers == {'Retry-After': str(retry)}
    for call in ip_check.await_args_list:
        assert call.kwargs['fail_closed'] is True
    if kind == 'resend' and window_index == 2:
        subject_check.assert_awaited_once_with(
            hashlib.sha256(b'user@example.com').hexdigest()[:32], 'email_resend',
            limit=4, window=3600, fail_closed=True,
        )
    else:
        subject_check.assert_not_awaited()
        assert ip_check.await_count == window_index + 1


def test_email_routes_forward_request_language(persian):
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names = {'enforce_email_registration_throttle', 'enforce_verification_resend_throttle'}
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id in names]
    assert len(calls) == 2
    capture = Mock()
    context = {name: capture for name in names}
    context.update(get_request_language=get_request_language, client_ip='127.0.0.1',
                   request=SimpleNamespace(email='test@example.com'),
                   raw_request=SimpleNamespace(headers={'Accept-Language': 'fa-IR'}))
    for call in calls:
        eval(compile(ast.Expression(call), str(path), 'eval'), context)
        assert capture.call_args.kwargs == {'language': 'fa'}


@pytest.mark.asyncio
@pytest.mark.parametrize('account', ['missing', 'verified', 'pending'])
async def test_public_resend_same_response(persian, account):
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                and node.name == 'resend_verification_public')
    node.decorator_list = []
    node.args.defaults = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             node], type_ignores=[])
    user = None if account == 'missing' else SimpleNamespace(
        language='fa', email_verified=account == 'verified', first_name='نام',
    )
    send = AsyncMock()
    context = dict(get_texts=get_texts, get_request_language=get_request_language,
                   require_email_auth_enabled=AsyncMock(), get_client_ip=lambda request: '127.0.0.1',
                   enforce_verification_resend_throttle=AsyncMock(), select=select, func=func, User=User,
                   _issue_verification_email=send)
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: user)))
    result = await context['resend_verification_public'](
        SimpleNamespace(email='test@example.com'), SimpleNamespace(headers={'Accept-Language': 'fa'}), db,
    )
    assert result == {'message': persian.t('CABINET_EMAIL_RESEND_PUBLIC_RESULT')}
    if account == 'pending':
        send.assert_awaited_once_with(db, user, language='fa', username='نام')
    else:
        send.assert_not_awaited()
