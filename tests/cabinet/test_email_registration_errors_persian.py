"""Registration failures retain guards and show real Persian catalog text."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, status
from sqlalchemy import func, select

from app.cabinet.auth import email_auth_gate
from app.cabinet.utils.request_language import get_request_language
from app.database.models import User
from app.localization.texts import get_texts
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('disabled', 403, 'CABINET_EMAIL_AUTH_DISABLED'),
    ('test_password', 400, 'CABINET_AUTH_INVALID_TEST_EMAIL_PASSWORD'),
    ('disposable', 400, 'CABINET_AUTH_DISPOSABLE_EMAIL_NOT_ALLOWED'),
    ('admin_email', 403, 'CABINET_AUTH_EMAIL_CANNOT_BE_USED_FOR_REGISTRATION'),
    ('duplicate', 400, 'CABINET_AUTH_EMAIL_ALREADY_REGISTERED'),
    ('alias', 400, 'CABINET_AUTH_EMAIL_ALREADY_REGISTERED'),
])
async def test_registration_failures(persian, monkeypatch, case, code, key):
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                and node.name == 'register_email_standalone')
    node.decorator_list = []
    node.args.defaults = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             node], type_ignores=[])
    monkeypatch.setattr(email_auth_gate, 'is_email_auth_enabled', AsyncMock(return_value=case != 'disabled'))
    throttle = AsyncMock()
    hash_password = Mock()
    context = dict(
        get_texts=get_texts, get_request_language=get_request_language, HTTPException=HTTPException, status=status,
        require_email_auth_enabled=email_auth_gate.require_email_auth_enabled,
        get_client_ip=lambda request: '127.0.0.1', enforce_email_registration_throttle=throttle,
        evaluate_public_registration=AsyncMock(), raise_for_registration_decision=Mock(),
        RegistrationChannel=SimpleNamespace(CABINET_EMAIL='email'), logger=Mock(),
        settings=SimpleNamespace(is_test_email=lambda email: case == 'test_password',
                                  validate_test_email_password=lambda *args: False,
                                  get_admin_emails=lambda: ['test@example.com'] if case == 'admin_email' else []),
        disposable_email_service=SimpleNamespace(is_disposable=lambda email: case == 'disposable'),
        select=select, func=func, User=User,
        get_user_by_email_alias=AsyncMock(return_value=SimpleNamespace(id=123) if case == 'alias' else None),
        hash_password=hash_password,
    )
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    request = SimpleNamespace(email='test@example.com', password='test', referral_code=None, campaign_slug=None, language='fa')
    raw = SimpleNamespace(headers={'Accept-Language': 'fa-IR'})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalar_one_or_none=lambda: object() if case == 'duplicate' else None)))
    with pytest.raises(HTTPException) as error:
        await context['register_email_standalone'](request, raw, db)
    assert error.value.status_code == code
    if case == 'disabled':
        assert error.value.detail == {'code': 'email_auth_disabled', 'message': persian.t(key)}
        throttle.assert_not_awaited()
    else:
        assert error.value.detail == persian.t(key)
        throttle.assert_awaited_once_with('127.0.0.1', language='fa')
    if case not in {'duplicate', 'alias'}:
        db.execute.assert_not_awaited()
    hash_password.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('disabled', 403, 'CABINET_EMAIL_AUTH_DISABLED'),
    ('limited', 429, 'CABINET_LOGIN_RATE_LIMIT'),
    ('missing', 401, 'CABINET_EMAIL_INVALID_CREDENTIALS'),
    ('wrong_password', 401, 'CABINET_EMAIL_INVALID_CREDENTIALS'),
    ('blocked', 401, 'CABINET_EMAIL_INVALID_CREDENTIALS'),
    ('deleted', 401, 'CABINET_EMAIL_INVALID_CREDENTIALS'),
    ('no_password', 401, 'CABINET_AUTH_PASSWORD_LOGIN_NOT_CONFIGURED'),
    ('unverified', 403, 'CABINET_AUTH_VERIFY_EMAIL_FIRST'),
])
async def test_login_failures(persian, monkeypatch, case, code, key):
    from app.database.models import UserStatus
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'login_email')
    node.decorator_list = []
    node.args.defaults = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             node], type_ignores=[])
    monkeypatch.setattr(email_auth_gate, 'is_email_auth_enabled', AsyncMock(return_value=case != 'disabled'))
    user = None if case == 'missing' else SimpleNamespace(
        password_hash=None if case == 'no_password' else 'hash', email_verified=False,
        status=case if case in {'blocked', 'deleted'} else 'active', language='en',
    )
    rate = AsyncMock(return_value=case == 'limited')
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: user)), commit=AsyncMock())
    verify = Mock(return_value=case != 'wrong_password')
    context = dict(get_texts=get_texts, get_request_language=get_request_language,
                   require_email_auth_enabled=email_auth_gate.require_email_auth_enabled,
                   HTTPException=HTTPException, status=status, UserStatus=UserStatus, User=User, select=select, func=func,
                   get_client_ip=lambda request: '127.0.0.1', RateLimitCache=SimpleNamespace(is_ip_rate_limited=rate),
                   settings=SimpleNamespace(is_test_email=lambda email: False,
                                             is_cabinet_email_verification_enabled=lambda: True),
                   verify_password=verify)
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    with pytest.raises(HTTPException) as error:
        await context['login_email'](SimpleNamespace(email='test@example.com', password='test'),
                                     SimpleNamespace(headers={'Accept-Language': 'fa'}), db)
    assert error.value.status_code == code
    expected = persian.t(key)
    assert error.value.detail == ({'code': 'email_auth_disabled', 'message': expected} if case == 'disabled' else expected)
    if case == 'limited':
        assert error.value.headers == {'Retry-After': '60'}
    if case in {'disabled', 'limited'}:
        db.execute.assert_not_awaited()
    if case == 'disabled':
        rate.assert_not_awaited()
    if case in {'blocked', 'deleted'}:
        verify.assert_called_once_with('test', 'hash')
    db.commit.assert_not_awaited()
