"""Exercise authenticated account errors without optional cabinet imports."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, status

from app.database.models import UserStatus
from app.localization.texts import get_texts
from app.cabinet.utils.request_language import get_request_language
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'key', 'code'), [
    ('mismatch', 'CABINET_AUTH_TELEGRAM_ACCOUNT_MISMATCH', 401),
    ('blacklisted', 'CABINET_AUTH_ACCESS_DENIED', 403),
    ('custom_blacklist', None, 403),
    ('deleted', 'CABINET_AUTH_ACCOUNT_DEACTIVATED', 403),
    ('inactive', 'CABINET_AUTH_ACCOUNT_NOT_ACTIVE', 403),
    ('maintenance', 'CABINET_AUTH_SERVICE_UNDER_MAINTENANCE', 503),
    ('channel', 'CABINET_AUTH_CHANNEL_SUBSCRIPTION_REQUIRED', 403),
])
async def test_authenticated_session_errors(persian, monkeypatch, case, key, code):
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/dependencies.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == 'get_current_cabinet_user')
    function.args.defaults = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             function], type_ignores=[])
    user = SimpleNamespace(id=123, language='fa', telegram_id=456, username='test', email=None, email_verified=False,
                           status=UserStatus.DELETED.value if case == 'deleted' else
                           UserStatus.BLOCKED.value if case == 'inactive' else UserStatus.ACTIVE.value)
    reason = SimpleNamespace(INVITE_ONLY_DISABLED='disabled', VERIFIED_ADMIN='admin')
    context = dict(
        get_texts=get_texts, get_request_language=get_request_language,
        HTTPException=HTTPException, status=status, UserStatus=UserStatus,
        logger=Mock(), get_token_payload=lambda *args, **kwargs: {'sub': '123'},
        get_user_by_id=AsyncMock(return_value=user),
        maintenance_service=SimpleNamespace(is_maintenance_active=lambda: case == 'maintenance',
                                            get_status_info=lambda: {'reason': None},
                                            get_maintenance_message=lambda language: None),
        validate_telegram_init_data=lambda *args, **kwargs: {'id': 999},
        blacklist_service=SimpleNamespace(is_user_blacklisted=AsyncMock(
            return_value=(case in {'blacklisted', 'custom_blacklist'},
                          'دلیل سفارشی مدیر' if case == 'custom_blacklist' else None))),
        settings=SimpleNamespace(INVITE_ONLY_ENABLED=False, CHANNEL_IS_REQUIRED_SUB=True,
                                  get_bot_username=lambda: 'test_bot',
                                  is_admin=lambda **kwargs: False),
        is_user_admin_by_env=lambda user: SimpleNamespace(is_admin=False),
        RegistrationAccessReason=reason,
        RegistrationAccessDecision=lambda allowed, reason: SimpleNamespace(allowed=allowed, reason=reason),
    )
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    channels = [{'is_subscribed': False, 'title': 'کانال آزمایشی', 'url': 'https://t.me/test_channel'}]
    if case == 'channel':
        from app.services.channel_subscription_service import channel_subscription_service
        monkeypatch.setattr(channel_subscription_service, 'get_channels_with_status', AsyncMock(return_value=channels))
    request = SimpleNamespace(headers={'X-Telegram-Init-Data': 'signed-data'} if case == 'mismatch' else {})
    db = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await context['get_current_cabinet_user'](request, SimpleNamespace(credentials='token'), db)
    assert error.value.status_code == code
    detail = error.value.detail
    message = detail['message'] if isinstance(detail, dict) else detail
    assert message == (persian.t(key) if key else 'دلیل سفارشی مدیر')
    if case == 'deleted':
        assert detail['code'] == 'account_deleted'
        assert detail['telegram_deep_link'] == 'https://t.me/test_bot?start=revive'
        assert '/start' in message
    if case == 'mismatch':
        assert error.value.headers == {'WWW-Authenticate': 'Bearer'}
    if case == 'channel':
        assert detail['code'] == 'channel_subscription_required' and detail['channels'] == channels
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(('header', 'expected'), [
    ('fa-IR, en;q=0.8', 'fa'), ('en;q=0.2,fa;q=0.9', 'fa'),
    ('fa;q=0,en', 'en'), ('fa;q=broken,en', 'en'),
    ('fa;q=NaN,en', 'en'), ('fa;q=2,en', 'en'),
    ('uk-UA', 'ua'), ('fa_IR', 'fa'), ('en,fa', 'en'),
    ('', 'ru'), ('xx,*', 'ru'),
])
def test_request_language(header, expected, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'ru')
    assert get_request_language(SimpleNamespace(headers={'Accept-Language': header})) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'key', 'code'), [
    ('missing', 'CABINET_AUTH_REQUIRED', 401),
    ('expired', 'CABINET_AUTH_INVALID_OR_EXPIRED_TOKEN', 401),
    ('malformed', 'CABINET_AUTH_INVALID_TOKEN_PAYLOAD', 401),
    ('unknown_user', 'CABINET_AUTH_USER_NOT_FOUND', 404),
])
async def test_preauth_errors(persian, case, key, code):
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/dependencies.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == 'get_current_cabinet_user')
    function.args.defaults = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             function], type_ignores=[])
    lookup = AsyncMock(return_value=None)
    context = dict(get_request_language=get_request_language, get_texts=get_texts,
                   HTTPException=HTTPException, status=status, get_user_by_id=lookup,
                   maintenance_service=SimpleNamespace(is_maintenance_active=lambda: False),
                   get_token_payload=lambda *args, **kwargs:
                   None if case == 'expired' else {'sub': 'invalid' if case == 'malformed' else '123'})
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    request = SimpleNamespace(headers={'Accept-Language': 'fa-IR,en;q=0.5'})
    with pytest.raises(HTTPException) as error:
        await context['get_current_cabinet_user'](
            request, None if case == 'missing' else SimpleNamespace(credentials='token'), AsyncMock())
    assert error.value.status_code == code and error.value.detail == persian.t(key)
    if code == 401:
        assert error.value.headers == {'WWW-Authenticate': 'Bearer'}
        lookup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('access', ['denied', 'environment', 'role'])
async def test_admin_access(persian, monkeypatch, access):
    from app.services import rbac_bootstrap_service
    from app.database.crud.rbac import UserRoleCRUD

    monkeypatch.setattr(rbac_bootstrap_service, 'is_user_admin_by_env',
                        lambda user: SimpleNamespace(is_admin=access == 'environment'))
    permissions = AsyncMock(return_value=(set(), [], 1 if access == 'role' else 0))
    monkeypatch.setattr(UserRoleCRUD, 'get_user_permissions', permissions)
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/dependencies.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == 'get_current_admin_user')
    function.args.defaults = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             function], type_ignores=[])
    context = dict(get_texts=get_texts, HTTPException=HTTPException, status=status)
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    user = SimpleNamespace(id=123, language='fa')
    if access == 'denied':
        with pytest.raises(HTTPException) as error:
            await context['get_current_admin_user'](object(), user, AsyncMock())
        assert error.value.status_code == 403
        assert error.value.detail == persian.t('CABINET_AUTH_ADMIN_ACCESS_REQUIRED')
    else:
        assert await context['get_current_admin_user'](object(), user, AsyncMock()) is user
    if access == 'environment':
        permissions.assert_not_awaited()
