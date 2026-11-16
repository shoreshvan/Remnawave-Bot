import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, status
from sqlalchemy import select

from app.database.models import User
from app.localization.texts import get_texts
from app.cabinet.utils.request_language import get_request_language
from tests.fixtures.persian import persian  # noqa: F401


@pytest.fixture
def functions():
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
             and node.name in {'verify_email', 'resend_verification'}]
    for node in nodes:
        node.decorator_list = []
        node.args.defaults = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             *nodes], type_ignores=[])
    context = dict(get_texts=get_texts, get_request_language=get_request_language,
                   HTTPException=HTTPException, status=status, select=select, User=User,
                   require_email_auth_enabled=AsyncMock())
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('no_email', 400, 'CABINET_AUTH_NO_EMAIL_TO_VERIFY'),
    ('verified', 400, 'CABINET_AUTH_EMAIL_ALREADY_VERIFIED'),
    ('disabled', 400, 'CABINET_AUTH_EMAIL_VERIFICATION_DISABLED'),
    ('unavailable', 503, 'CABINET_AUTH_EMAIL_SERVICE_NOT_CONFIGURED'),
    ('sent', None, 'CABINET_AUTH_VERIFICATION_EMAIL_SENT'),
])
async def test_resend(persian, functions, case, code, key):
    send = AsyncMock(return_value=case == 'sent')
    functions.update(_issue_verification_email=send,
                     settings=SimpleNamespace(is_cabinet_email_verification_enabled=lambda: case != 'disabled'))
    user = SimpleNamespace(language='fa', email=None if case == 'no_email' else 'test@example.com',
                           email_verified=case == 'verified', first_name='نام')
    db = AsyncMock()
    if code:
        with pytest.raises(HTTPException) as error:
            await functions['resend_verification'](user, db)
        assert error.value.status_code == code and error.value.detail == persian.t(key)
    else:
        assert await functions['resend_verification'](user, db) == {'message': persian.t(key)}
    functions['require_email_auth_enabled'].assert_awaited_once_with(db, language='fa')
    if case in {'no_email', 'verified'}:
        send.assert_not_awaited()
    else:
        send.assert_awaited_once_with(db, user, language='fa', username='نام')


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('limited', 429, 'CABINET_LOGIN_RATE_LIMIT'),
    ('missing', 400, 'CABINET_EMAIL_INVALID_VERIFICATION_TOKEN'),
    ('expired', 400, 'CABINET_AUTH_VERIFICATION_TOKEN_EXPIRED'),
])
async def test_verify_errors(persian, functions, case, code, key):
    user = SimpleNamespace(email_verification_expires='expired', email_verified=False)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalar_one_or_none=lambda: None if case == 'missing' else user)), commit=AsyncMock())
    rate = AsyncMock(return_value=case == 'limited')
    functions.update(get_client_ip=lambda request: '127.0.0.1',
                     RateLimitCache=SimpleNamespace(is_ip_rate_limited=rate), is_token_expired=lambda value: True)
    with pytest.raises(HTTPException) as error:
        await functions['verify_email'](SimpleNamespace(token='token'),
                                        SimpleNamespace(headers={'Accept-Language': 'fa'}), db)
    assert error.value.status_code == code and error.value.detail == persian.t(key)
    assert user.email_verified is False
    db.commit.assert_not_awaited()
    rate.assert_awaited_once_with('127.0.0.1', 'email_verify', limit=10, window=60, fail_closed=True)
    if case == 'limited':
        assert error.value.headers == {'Retry-After': '60'}
        db.execute.assert_not_awaited()


@pytest.mark.parametrize('required', [True, False])
def test_registration_success_message(persian, required):
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == 'register_email_standalone')
    response = next(node for node in ast.walk(function) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == 'RegisterResponse')
    expression = next(keyword.value for keyword in response.keywords if keyword.arg == 'message')
    message = eval(compile(ast.Expression(expression), str(path), 'eval'),
                   dict(texts=persian, verification_required=required))
    assert message == persian.t('CABINET_AUTH_VERIFICATION_EMAIL_SENT_INBOX' if required
                               else 'CABINET_EMAIL_REGISTRATION_COMPLETE')
