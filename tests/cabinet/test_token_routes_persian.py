import ast
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, status
from sqlalchemy import select

from app.database.models import CabinetRefreshToken, UserStatus
from app.localization.texts import get_texts
from app.cabinet.utils.request_language import get_request_language
from tests.fixtures.persian import persian  # noqa: F401


@pytest.fixture
def routes():
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
             and node.name in {'refresh_token', 'logout', 'auto_login'}]
    for node in nodes:
        node.decorator_list = []
        node.args.defaults = []
    context = dict(get_texts=get_texts, get_request_language=get_request_language, HTTPException=HTTPException,
                   status=status, select=select, CabinetRefreshToken=CabinetRefreshToken, UserStatus=UserStatus,
                   hashlib=hashlib, datetime=datetime, UTC=UTC, logger=Mock())
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize(('route', 'case', 'code', 'key'), [
    ('refresh_token', 'invalid', 401, 'CABINET_REFRESH_INVALID'),
    ('refresh_token', 'malformed', 401, 'CABINET_AUTH_INVALID_TOKEN_PAYLOAD'),
    ('refresh_token', 'revoked', 401, 'CABINET_REFRESH_REVOKED'),
    ('refresh_token', 'expired', 401, 'CABINET_REFRESH_EXPIRED'),
    ('refresh_token', 'missing', 401, 'CABINET_REFRESH_USER_UNAVAILABLE'),
    ('refresh_token', 'inactive', 401, 'CABINET_REFRESH_USER_UNAVAILABLE'),
    ('auto_login', 'limited', 429, 'CABINET_LOGIN_RATE_LIMIT'),
    ('auto_login', 'invalid', 401, 'CABINET_AUTO_LOGIN_INVALID'),
    ('auto_login', 'malformed', 401, 'CABINET_AUTH_INVALID_TOKEN_PAYLOAD'),
    ('auto_login', 'missing', 401, 'CABINET_AUTH_USER_NOT_FOUND'),
    ('auto_login', 'inactive', 403, 'CABINET_AUTH_ACCOUNT_NOT_ACTIVE'),
    ('auto_login', 'admin', 403, 'CABINET_AUTO_LOGIN_ADMIN_DENIED'),
])
async def test_errors(persian, routes, route, case, code, key):
    user = SimpleNamespace(id=123, telegram_id=456, status='blocked' if case == 'inactive' else 'active')
    record = None if case == 'revoked' else SimpleNamespace(is_valid=case != 'expired')
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: record)), commit=AsyncMock())
    mint = AsyncMock()
    payload = Mock(return_value=None if case == 'invalid' else {'sub': 'bad' if case == 'malformed' else '123'})
    routes.update(get_token_payload=payload, get_user_by_id=AsyncMock(return_value=None if case == 'missing' else user),
                  get_client_ip=lambda request: '127.0.0.1',
                  RateLimitCache=SimpleNamespace(is_ip_rate_limited=AsyncMock(return_value=case == 'limited')),
                  is_user_admin_by_env=lambda user: SimpleNamespace(is_admin=case == 'admin'),
                  _create_auth_response=mint, create_access_token=mint)
    with pytest.raises(HTTPException) as error:
        await routes[route](SimpleNamespace(refresh_token='token', token='token'),
                            SimpleNamespace(headers={'Accept-Language': 'fa'}), db)
    assert error.value.status_code == code and error.value.detail == persian.t(key)
    if case == 'limited':
        assert error.value.headers == {'Retry-After': '60'}
        payload.assert_not_called()
    else:
        payload.assert_called_once_with('token', expected_type='refresh' if route == 'refresh_token' else 'auto_login')
    mint.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('found', [True, False])
async def test_logout(persian, routes, found):
    record = SimpleNamespace(revoked_at=None) if found else None
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: record)), commit=AsyncMock())
    result = await routes['logout'](SimpleNamespace(refresh_token='token'),
                                    SimpleNamespace(headers={'Accept-Language': 'fa'}), db)
    assert result == {'message': persian.t('CABINET_LOGOUT_SUCCESS')}
    params = db.execute.await_args.args[0].compile().params
    assert hashlib.sha256(b'token').hexdigest() in params.values()
    if found:
        assert record.revoked_at is not None
        db.commit.assert_awaited_once()
    else:
        db.commit.assert_not_awaited()
