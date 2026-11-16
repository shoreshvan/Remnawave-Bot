from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.cabinet.routes import auth
from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.database.models import User
from tests.fixtures.persian import persian  # noqa: F401
from tests.fixtures.sqlite_memory import memory_session


def make_app(user, db):
    app = FastAPI()
    app.include_router(auth.router, prefix='/cabinet')
    app.dependency_overrides[get_current_cabinet_user] = lambda: user
    app.dependency_overrides[get_cabinet_db] = lambda: db
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(('path', 'body', 'key'), [
    ('', {}, 'CABINET_INPUT_REQUIRED'),
    ('', {'new_email': 'bad'}, 'CABINET_INPUT_EMAIL_INVALID'),
    ('', {'new_email': 123}, 'CABINET_INPUT_EMAIL_INVALID'),
    ('', [], 'CABINET_INPUT_BODY_INVALID'),
    ('/verify', {}, 'CABINET_INPUT_REQUIRED'),
    ('/verify', {'code': '123'}, 'CABINET_INPUT_CODE_INVALID'),
    ('/verify', {'code': '1234567'}, 'CABINET_INPUT_CODE_INVALID'),
    ('/verify', {'code': 'abcdef'}, 'CABINET_INPUT_CODE_INVALID'),
    ('/verify', {'code': 123456}, 'CABINET_INPUT_CODE_INVALID'),
])
async def test_validation_http(persian, path, body, key):
    db = AsyncMock()
    app = make_app(SimpleNamespace(language='fa'), db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/cabinet/auth/email/change' + path, json=body, headers={'Accept-Language': 'fa'})
    assert response.status_code == 422
    errors = response.json()['detail']
    assert all(item['msg'] == persian.t(key) for item in errors)
    assert all('type' in item and 'loc' in item for item in errors)
    if 'reason' in errors[0].get('ctx', {}):
        assert errors[0]['ctx']['reason'] == persian.t(key)
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_json_and_other_language(persian):
    app = make_app(SimpleNamespace(language='fa'), AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/cabinet/auth/email/change', content='{',
            headers={'Accept-Language': 'fa', 'Content-Type': 'application/json'})
        other = await client.post('/cabinet/auth/email/change', json={}, headers={'Accept-Language': 'en'})
    assert response.status_code == 422
    assert response.json()['detail'][0]['msg'] == persian.t('CABINET_INPUT_JSON_INVALID')
    assert other.status_code == 422 and other.json()['detail'][0]['msg'] == 'Field required'


@pytest.mark.asyncio
@pytest.mark.parametrize('delivery', ['sent', 'failed', 'exception'])
async def test_real_http_change_lifecycle(persian, monkeypatch, delivery):
    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(type(auth.settings), 'get_admin_emails', lambda self: [])
    monkeypatch.setattr(auth.disposable_email_service, 'is_disposable', lambda email: False)
    monkeypatch.setattr(auth.email_service, 'is_configured', lambda: True)
    sender = Mock(return_value=delivery == 'sent',
                  side_effect=RuntimeError('private SMTP diagnostic') if delivery == 'exception' else None)
    monkeypatch.setattr(auth.email_service, 'send_email_change_code', sender)
    monkeypatch.setattr(auth, 'get_rendered_override', AsyncMock(return_value=None))
    monkeypatch.setattr(auth, 'generate_email_change_code', lambda: '012345')
    async with memory_session(monkeypatch, [User.__table__]) as db:
        user = User(email='old@example.com', email_verified=True, language='fa', balance_kopeks=125000)
        db.add(user)
        await db.commit()
        app = make_app(user, db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                               headers={'Accept-Language': 'fa'}) as client:
            requested = await client.post('/cabinet/auth/email/change', json={'new_email': 'new@example.com'})
            assert requested.status_code == 200, requested.text
            key = 'CABINET_AUTH_EMAIL_CHANGE_CODE_SENT' if delivery == 'sent' else 'CABINET_AUTH_MERGE_CODE_NOT_SENT'
            assert requested.json()['message'] == persian.t(key)
            assert user.email == 'old@example.com' and user.email_change_code == '012345'
            status = await client.get('/cabinet/auth/email/change/status')
            assert status.json()['pending'] is True
            wrong = await client.post('/cabinet/auth/email/change/verify', json={'code': '999999'})
            assert wrong.status_code == 400
            assert wrong.json()['detail'] == persian.t('CABINET_AUTH_INVALID_CONFIRMATION_CODE')
            if delivery == 'sent':
                verified = await client.post('/cabinet/auth/email/change/verify', json={'code': '012345'})
                assert verified.status_code == 200, verified.text
                assert verified.json()['message'] == persian.t('CABINET_EMAIL_CHANGE_SUCCESS')
                assert user.email == 'new@example.com' and user.email_verified
            else:
                cancelled = await client.post('/cabinet/auth/email/change/cancel')
                assert cancelled.status_code == 200
                assert cancelled.json()['message'] == persian.t('CABINET_AUTH_EMAIL_CHANGE_CANCELLED')
                assert user.email == 'old@example.com'
            assert (await client.get('/cabinet/auth/email/change/status')).json()['pending'] is False
        assert user.balance_kopeks == 125000 and user.email_change_code is None
        sender.assert_called_once()
        assert sender.call_args.kwargs['language'] == 'fa'
