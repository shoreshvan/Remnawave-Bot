import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, status

from app.localization.texts import get_texts
from tests.fixtures.persian import persian  # noqa: F401


@pytest.fixture
def routes():
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/account_linking.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
             and node.name in {'get_merge_preview_endpoint', 'execute_merge_endpoint'}]
    for node in nodes:
        node.decorator_list = []
        node.args.defaults = []
    context = dict(get_texts=get_texts, HTTPException=HTTPException, status=status,
                   datetime=datetime, UTC=UTC, logger=Mock(), get_client_ip=lambda request: '127.0.0.1',
                   RateLimitCache=SimpleNamespace(is_ip_rate_limited=AsyncMock(return_value=False)),
                   MERGE_TOKEN_TTL_SECONDS=600, MergePreviewResponse=SimpleNamespace, MergePreviewUser=SimpleNamespace)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('limited', 429, 'CABINET_TOO_MANY_REQUESTS'),
    ('missing', 404, 'CABINET_MERGE_TOKEN_CONSUMED'),
    ('other_user', 403, 'CABINET_MERGE_ONLY_INITIATOR'),
    ('invalid_selection', 400, 'CABINET_MERGE_KEEP_FROM_INVALID'),
    ('value_error', 400, 'CABINET_MERGE_CANNOT_COMPLETE'),
    ('internal', 500, 'CABINET_MERGE_INTERNAL_ERROR'),
])
async def test_execution_errors(persian, routes, case, code, key):
    token = dict(primary_user_id=123, secondary_user_id=456, provider='email', provider_id='test@example.com')
    consume = AsyncMock(return_value=None if case == 'missing' else token)
    restore = AsyncMock()
    execute = AsyncMock(side_effect=ValueError('private diagnostic') if case == 'value_error' else RuntimeError('private diagnostic'))
    flush = AsyncMock()
    routes.update(consume_merge_token=consume, restore_merge_token=restore, execute_merge=execute,
                  flush_remnawave_deletions=flush)
    routes['RateLimitCache'].is_ip_rate_limited.return_value = case == 'limited'
    db = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await routes['execute_merge_endpoint'](
            SimpleNamespace(keep_subscription_from=999 if case == 'invalid_selection' else 456), object(), 'token',
            SimpleNamespace(id=999 if case == 'other_user' else 123, language='fa'), db)
    assert error.value.status_code == code and error.value.detail == persian.t(key)
    assert 'private diagnostic' not in error.value.detail
    if case in {'other_user', 'invalid_selection', 'value_error', 'internal'}:
        restore.assert_awaited_once_with('token', token)
    else:
        restore.assert_not_awaited()
    if case in {'value_error', 'internal'}:
        db.rollback.assert_awaited_once()
        assert execute.await_args.kwargs['keep_subscription_from'] == 'secondary'
    else:
        execute.assert_not_awaited()
    flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('limited', 429, 'CABINET_TOO_MANY_REQUESTS'), ('missing', 404, 'CABINET_MERGE_TOKEN_INVALID'),
    ('other_user', 403, 'CABINET_MERGE_ONLY_INITIATOR'), ('missing_users', 404, 'CABINET_MERGE_USERS_NOT_FOUND'),
    ('success', None, None),
])
async def test_preview(persian, routes, case, code, key):
    token = dict(primary_user_id=123, secondary_user_id=456, created_at=datetime.now(UTC).isoformat())
    preview = {'primary': {'id': 123, 'balance_kopeks': 125000}, 'secondary': {'id': 456, 'balance_kopeks': 250000}}
    get_preview = AsyncMock(return_value=preview, side_effect=ValueError('private') if case == 'missing_users' else None)
    routes.update(get_merge_token_data=AsyncMock(return_value=None if case == 'missing' else token), get_merge_preview=get_preview)
    routes['RateLimitCache'].is_ip_rate_limited.return_value = case == 'limited'
    user = SimpleNamespace(id=999 if case == 'other_user' else 123, language='fa')
    db = AsyncMock()
    if code:
        with pytest.raises(HTTPException) as error:
            await routes['get_merge_preview_endpoint'](object(), 'token', user, db)
        assert error.value.status_code == code and error.value.detail == persian.t(key)
    else:
        result = await routes['get_merge_preview_endpoint'](object(), 'token', user, db)
        assert result.primary.balance_kopeks == 125000 and result.secondary.balance_kopeks == 250000
        assert 590 <= result.expires_in_seconds <= 600
        get_preview.assert_awaited_once_with(db, 123, 456)
    if case in {'limited', 'missing', 'other_user'}:
        get_preview.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['grace', 'commit_failed', 'load_failed', 'session_failed', 'success', 'resync_failed'])
async def test_merge_completion(persian, monkeypatch, routes, case):
    from app.services.grace_access_runtime import GraceAccessDeletionBlocked
    from app.services import remnawave_resync_service

    events = []
    user = SimpleNamespace(id=123, language='fa')
    token = dict(primary_user_id=123, secondary_user_id=456, provider='email', provider_id='test@example.com')
    async def execute(**kwargs):
        events.append('merge')
        kwargs['deferred_remnawave_deletions'].append(789)
        if case == 'grace':
            raise GraceAccessDeletionBlocked([789])
        return user
    async def commit():
        events.append('commit')
        if case == 'commit_failed':
            raise RuntimeError('private diagnostic')
    async def flush(ids):
        assert ids == [789]
        events.append('flush')
    resync = AsyncMock(return_value={'synced': 1, 'failed': 0},
                       side_effect=RuntimeError('private diagnostic') if case == 'resync_failed' else None)
    monkeypatch.setattr(remnawave_resync_service, 'resync_user_subscriptions_with_panel', resync)
    restore, store = AsyncMock(), AsyncMock()
    auth = AsyncMock(return_value=SimpleNamespace(access_token='access', refresh_token='refresh'),
                     side_effect=RuntimeError('private diagnostic') if case == 'session_failed' else None)
    routes.update(consume_merge_token=AsyncMock(return_value=token), restore_merge_token=restore,
                  execute_merge=execute, flush_remnawave_deletions=AsyncMock(side_effect=flush),
                  get_user_by_id=AsyncMock(return_value=None if case == 'load_failed' else user),
                  _create_auth_response=auth, _store_refresh_token=store,
                  _user_to_response=lambda value: {'id': value.id}, MergeResponse=SimpleNamespace)
    db = SimpleNamespace(commit=AsyncMock(side_effect=commit), rollback=AsyncMock())
    call = routes['execute_merge_endpoint'](SimpleNamespace(keep_subscription_from=123), object(), 'token', user, db)
    failures = {'grace': (409, 'CABINET_MERGE_BLOCKED_GRACE'),
                'commit_failed': (500, 'CABINET_MERGE_INTERNAL_ERROR'),
                'load_failed': (500, 'CABINET_MERGE_LOAD_FAILED'),
                'session_failed': (500, 'CABINET_MERGE_SESSION_FAILED')}
    if case in failures:
        with pytest.raises(HTTPException) as error:
            await call
        code, key = failures[case]
        assert error.value.status_code == code and error.value.detail == persian.t(key)
        assert '\\u200c' not in error.value.detail
    else:
        result = await call
        assert result.success is True and result.user == {'id': 123}
        assert result.access_token == 'access' and result.refresh_token == 'refresh'
        store.assert_awaited_once_with(db, 123, 'refresh', device_info='merge')
    if case in {'grace', 'commit_failed'}:
        restore.assert_awaited_once_with('token', token)
        db.rollback.assert_awaited_once()
        routes['flush_remnawave_deletions'].assert_not_awaited()
        auth.assert_not_awaited()
    else:
        assert events == ['merge', 'commit', 'flush']
        restore.assert_not_awaited()
        db.rollback.assert_not_awaited()
