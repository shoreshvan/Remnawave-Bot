import ast
import hmac
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, status

from app.localization.texts import get_texts
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('ip_limit', 429, 'CABINET_LOGIN_RATE_LIMIT'),
    ('user_limit', 429, 'CABINET_AUTH_MERGE_TOO_MANY_ATTEMPTS'),
    ('no_pending', 400, 'CABINET_AUTH_NO_PENDING_MERGE'),
    ('wrong_code', 400, 'CABINET_AUTH_INVALID_CONFIRMATION_CODE'),
    ('missing_target', 409, 'CABINET_AUTH_MERGE_TARGET_UNAVAILABLE'),
    ('same_account', 409, 'CABINET_AUTH_MERGE_TARGET_UNAVAILABLE'),
    ('changed_email', 409, 'CABINET_AUTH_MERGE_TARGET_UNAVAILABLE'),
    ('success', None, 'CABINET_AUTH_MERGE_CONFIRMED'),
])
async def test_merge_code_flow(persian, case, code, key):
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                and node.name == 'verify_email_merge')
    node.decorator_list = []
    node.args.defaults = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             node], type_ignores=[])
    clear, mint = AsyncMock(), AsyncMock(return_value='merge-token')
    rate = AsyncMock(side_effect=[case == 'ip_limit', case == 'user_limit'])
    pending = None if case == 'no_pending' else {'code': '123456', 'secondary_user_id': 456, 'email': 'test@example.com'}
    secondary = None if case == 'missing_target' else SimpleNamespace(
        id=123 if case == 'same_account' else 456,
        email='other@example.com' if case == 'changed_email' else ' Test@Example.com ')
    lookup = AsyncMock(return_value=secondary)
    context = dict(get_texts=get_texts, HTTPException=HTTPException, status=status, hmac=hmac, logger=Mock(),
                   require_email_auth_enabled=AsyncMock(), get_client_ip=lambda request: '127.0.0.1',
                   RateLimitCache=SimpleNamespace(is_ip_rate_limited=rate),
                   get_email_merge_otp=AsyncMock(return_value=pending), clear_email_merge_otp=clear,
                   get_user_by_id=lookup, create_merge_token=mint)
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    user, db = SimpleNamespace(id=123, language='fa'), AsyncMock()
    request = SimpleNamespace(code='000000' if case == 'wrong_code' else '123456')
    if code:
        with pytest.raises(HTTPException) as error:
            await context['verify_email_merge'](request, object(), user, db)
        assert error.value.status_code == code and error.value.detail == persian.t(key)
        mint.assert_not_awaited()
        if case == 'ip_limit':
            assert error.value.headers == {'Retry-After': '60'}
    else:
        result = await context['verify_email_merge'](request, object(), user, db)
        assert result == {'message': persian.t(key), 'merge_required': True, 'merge_token': 'merge-token'}
        mint.assert_awaited_once_with(primary_user_id=123, secondary_user_id=456,
                                     provider='email', provider_id='test@example.com')
    if case in {'user_limit', 'missing_target', 'same_account', 'changed_email', 'success'}:
        clear.assert_awaited_once_with(123)
    else:
        clear.assert_not_awaited()
    if case in {'ip_limit', 'user_limit', 'no_pending', 'wrong_code'}:
        lookup.assert_not_awaited()
    for call in rate.await_args_list:
        assert call.kwargs['fail_closed'] is True and call.kwargs['limit'] == 5
    context['require_email_auth_enabled'].assert_awaited_once_with(db, language='fa')
