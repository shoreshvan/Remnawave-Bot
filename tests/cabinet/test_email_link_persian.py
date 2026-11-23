import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, status
from sqlalchemy import select, func

from app.database.models import User, UserStatus
from app.localization.texts import get_texts
from app.cabinet.utils.request_language import get_request_language
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('limited', 429, 'CABINET_LOGIN_RATE_LIMIT'),
    ('verified', 400, 'CABINET_AUTH_ALREADY_HAVE_VERIFIED_EMAIL'),
    ('disposable', 400, 'CABINET_AUTH_DISPOSABLE_EMAIL_NOT_ALLOWED'),
    ('admin', 403, 'CABINET_AUTH_EMAIL_CANNOT_BE_LINKED'),
    ('same', 400, 'CABINET_AUTH_EMAIL_ALREADY_LINKED'),
    ('unconfigured', 503, 'CABINET_AUTH_EMAIL_SERVICE_NOT_CONFIGURED_MERGE'),
    ('merge', None, 'CABINET_AUTH_MERGE_CODE_SENT'),
    ('merge_send_failed', None, 'CABINET_AUTH_MERGE_CODE_NOT_SENT'),
    ('merge_other_language', None, 'CABINET_AUTH_MERGE_CODE_SENT'),
    ('link', None, 'CABINET_AUTH_EMAIL_LINKED_SUCCESS'),
    ('verify', None, 'CABINET_AUTH_VERIFICATION_EMAIL_SENT'),
])
async def test_link_email(persian, case, code, key):
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'register_email')
    node.decorator_list = []
    node.args.defaults = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             node], type_ignores=[])
    user = SimpleNamespace(id=123, language='fa', email='old@example.com', email_verified=case == 'verified',
                           first_name='نام', password_hash='old-hash')
    target = SimpleNamespace(id=123 if case == 'same' else 456, language='en' if case == 'merge_other_language' else 'fa')
    existing = target if case in {'same', 'unconfigured'} or case.startswith('merge') else None
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: existing)), commit=AsyncMock())
    store, render, thread = AsyncMock(), AsyncMock(return_value=None), AsyncMock(return_value=case != 'merge_send_failed')
    sender = SimpleNamespace(is_configured=lambda: case != 'unconfigured',
                             send_email_change_code=Mock(), send_verification_email=Mock())
    context = dict(get_texts=get_texts, get_request_language=get_request_language, HTTPException=HTTPException,
        status=status, User=User, UserStatus=UserStatus, select=select, func=func, datetime=datetime, UTC=UTC,
        require_email_auth_enabled=AsyncMock(), get_client_ip=lambda request: '127.0.0.1',
        RateLimitCache=SimpleNamespace(is_ip_rate_limited=AsyncMock(return_value=case == 'limited')),
        disposable_email_service=SimpleNamespace(is_disposable=lambda email: case == 'disposable'),
        settings=SimpleNamespace(get_admin_emails=lambda: ['new@example.com'] if case == 'admin' else [],
            is_cabinet_email_verification_enabled=lambda: case == 'verify',
            get_cabinet_email_change_code_expire_minutes=lambda: 10,
            get_cabinet_email_verification_expire_hours=lambda: 2, CABINET_URL='https://example.test'),
        email_service=sender, generate_email_change_code=lambda: '123456', store_email_merge_otp=store,
        get_rendered_override=render, asyncio=SimpleNamespace(to_thread=thread), logger=Mock(),
        hash_password=lambda password: 'new-hash', generate_verification_token=lambda: 'token',
        get_verification_expires_at=lambda: 'expiry')
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    args = (SimpleNamespace(email='new@example.com', password='password'),
            SimpleNamespace(headers={'Accept-Language': 'fa'}), user, db)
    if code:
        with pytest.raises(HTTPException) as error:
            await context['register_email'](*args)
        assert error.value.status_code == code and error.value.detail == persian.t(key)
        store.assert_not_awaited()
        thread.assert_not_awaited()
    else:
        result = await context['register_email'](*args)
        assert result['message'] == persian.t(key)
        if case.startswith('merge'):
            assert result['merge_required'] and result['merge_token'] is None
            assert result['merge_verification'] == 'email_code'
            store.assert_awaited_once_with(123, 456, 'new@example.com', '123456')
            language = 'en' if case == 'merge_other_language' else 'fa'
            assert render.await_args.args == ('email_change_code', language)
            assert thread.await_args.kwargs['language'] == language
            assert thread.await_args.kwargs['to_email'] == 'new@example.com'
        else:
            assert user.email == 'new@example.com' and user.password_hash == 'new-hash'
            assert user.email_verified == (case == 'link')
            db.commit.assert_awaited_once()
            if case == 'verify':
                assert user.email_verification_token == 'token'
                assert thread.await_args.kwargs['language'] == 'fa'
                assert thread.await_args.kwargs['verification_token'] == 'token'
    if code or case.startswith('merge'):
        assert user.email == 'old@example.com' and user.password_hash == 'old-hash'
        db.commit.assert_not_awaited()


@pytest.mark.parametrize('minutes', [1, 10])
@pytest.mark.parametrize('username', [None, 'نام <کاربر>'])
def test_real_confirmation_email(persian, monkeypatch, minutes, username):
    from app.cabinet.services.email_service import EmailService
    from app.config import settings

    monkeypatch.setattr(type(settings), 'get_cabinet_email_change_code_expire_minutes', lambda self: minutes)
    service = EmailService()
    send = Mock(return_value=True)
    monkeypatch.setattr(service, 'send_email', send)
    before = datetime.now(UTC)
    assert service.send_email_change_code('test@example.com', '012345', username=username, language='fa') is True
    recipient, subject, body = send.call_args.args
    assert recipient == 'test@example.com' and subject == 'کد تأیید نشانی ایمیل'
    assert '<html lang="fa" dir="rtl">' in body
    assert 'برای تأیید مالکیت این نشانی ایمیل' in body
    assert 'درخواست تغییر ایمیل' not in body
    assert 'کد را در اختیار دیگران نگذارید' in body
    assert f'این کد تا {minutes} دقیقه معتبر است.' in body
    assert '<p dir="ltr"' in body and '>012345</p>' in body
    assert 0 <= (send.call_args.kwargs['retry_until'] - before).total_seconds() - minutes * 60 < 5
    if username:
        assert 'نام &lt;کاربر&gt;' in body and 'نام <کاربر>' not in body
    assert not any('\u0400' <= char <= '\u04ff' for char in subject + body)


@pytest.mark.parametrize('sent', [False, True])
def test_custom_confirmation_email_preserved(persian, monkeypatch, sent):
    from app.cabinet.services.email_service import EmailService

    service = EmailService()
    send = Mock(return_value=sent)
    monkeypatch.setattr(service, 'send_email', send)
    assert service.send_email_change_code('test@example.com', '012345', language='fa',
        custom_subject='عنوان مدیر', custom_body_html='<p>متن سفارشی 012345</p>') is sent
    assert send.call_args.args == ('test@example.com', 'عنوان مدیر', '<p>متن سفارشی 012345</p>')


def test_confirmation_template_editor_description():
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/admin_email_templates.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    entries = [node for node in ast.walk(tree) if isinstance(node, ast.Dict)
               and any(isinstance(value, ast.Constant) and value.value == 'email_change_code'
                       for value in node.values)]
    entry = next(ast.literal_eval(node) for node in entries
                 if any(isinstance(key, ast.Constant) and key.value == 'label' for key in node.keys))
    assert entry['label']['fa'] == 'کد تأیید نشانی ایمیل'
    assert 'تغییر ایمیل یا ادغام حساب‌ها' in entry['description']['fa']
    assert entry['context_vars'] == ['username', 'code', 'expire_minutes']
