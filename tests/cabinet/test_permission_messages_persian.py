"""Check actual permission dependencies with isolated service calls."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, status

from app.localization.texts import get_texts
from app.services.permission_service import PermissionService
from tests.fixtures.persian import persian  # noqa: F401


@pytest.fixture
def permission_dependency():
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/dependencies.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in {'require_permission', '_permission_denied_message'}]
    assert len(nodes) == 2
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.AsyncFunctionDef):
                child.args.defaults = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             *nodes], type_ignores=[])
    context = dict(get_texts=get_texts, HTTPException=HTTPException, status=status,
                   get_client_ip=lambda request: '127.0.0.1')
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    return context['require_permission']


@pytest.mark.asyncio
@pytest.mark.parametrize(('reason', 'key', 'policy'), [
    ('No active roles assigned', 'CABINET_PERMISSION_NO_ACTIVE_ROLE', ''),
    ('Permission not granted by any role', 'CABINET_PERMISSION_ROLE_DENIED', ''),
    ('Denied by policy: محدودیت ساعت', 'CABINET_PERMISSION_POLICY_DENIED', 'محدودیت ساعت'),
    ('Denied by policy: Office <A> {rule}', 'CABINET_PERMISSION_POLICY_DENIED', 'Office <A> {rule}'),
    ('Unexpected internal reason', 'CABINET_PERMISSION_DENIED_UNKNOWN', ''),
])
async def test_denial_message_and_audit(persian, monkeypatch, permission_dependency, reason, key, policy):
    checker = AsyncMock(side_effect=[(True, 'Granted by RBAC'), (False, reason)])
    audit = AsyncMock()
    monkeypatch.setattr(PermissionService, 'check_permission', checker)
    monkeypatch.setattr(PermissionService, 'log_action', audit)
    request = SimpleNamespace(headers={'user-agent': 'test'}, method='GET', url=SimpleNamespace(path='/users'))
    user, db = SimpleNamespace(id=123, language='fa'), AsyncMock()
    dependency = permission_dependency('users:read', 'users:write', 'users:delete')
    with pytest.raises(HTTPException) as error:
        await dependency(request, user, db)
    assert error.value.status_code == 403
    assert error.value.detail == persian.t(key).format(policy=policy)
    assert checker.await_count == 2
    assert checker.await_args.args == (db, user, 'users:write')
    assert audit.await_args.kwargs['details'] == {'reason': reason}
    assert audit.await_args.kwargs['status'] == 'denied'
    assert audit.await_args.kwargs['action'] == 'users:write'
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_allowed_permissions_unchanged(persian, monkeypatch, permission_dependency):
    checker = AsyncMock(return_value=(True, 'Granted by RBAC'))
    audit = AsyncMock()
    monkeypatch.setattr(PermissionService, 'check_permission', checker)
    monkeypatch.setattr(PermissionService, 'log_action', audit)
    request = SimpleNamespace(headers={}, method='GET', url=SimpleNamespace(path='/users'), query_params={})
    user, db = SimpleNamespace(id=123, language='fa'), AsyncMock()
    assert await permission_dependency('users:read', 'users:write')(request, user, db) is user
    assert checker.await_count == 2
    assert audit.await_args.kwargs['status'] == 'success'
    assert audit.await_args.kwargs['action'] == 'users:read,users:write'
    db.commit.assert_awaited_once()
