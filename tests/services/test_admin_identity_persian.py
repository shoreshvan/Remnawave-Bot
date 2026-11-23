"""Admin identity labels follow the recipient, including missing account data."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.admin_notification_service import AdminNotificationService
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.parametrize(('fields', 'display', 'identifier', 'label'), [
    ({'id': 7}, 'کاربر شمارهٔ 7', 'کاربر شمارهٔ 7', 'شناسه'),
    ({}, 'نامشخص', 'نامشخص', 'شناسه'),
    ({'telegram_id': 123}, 'شناسهٔ تلگرام 123', '<code>123</code>', 'شناسه تلگرام'),
    ({'email': 'a&b@example.com'}, 'a&amp;b@example.com', '📧 a&amp;b@example.com', 'ایمیل'),
    ({'first_name': '<نام>', 'id': 7}, '&lt;نام&gt;', 'کاربر شمارهٔ 7', 'شناسه'),
    ({'username': '<login>', 'id': 7}, '&lt;login&gt;', 'کاربر شمارهٔ 7', 'شناسه'),
])
def test_identity_fields_are_persian_and_escaped(persian, fields, display, identifier, label):
    service = AdminNotificationService(SimpleNamespace())
    user = SimpleNamespace(language='ru', **fields)
    assert service._get_user_display(user, texts=persian) == display
    assert service._get_user_identifier_display(user, texts=persian) == identifier
    assert service._get_user_identifier_label(user, texts=persian) == label


@pytest.mark.asyncio
async def test_addon_notification_passes_recipient_language_to_identity(persian, monkeypatch):
    service = AdminNotificationService(SimpleNamespace())
    monkeypatch.setattr(service, '_is_enabled', lambda: True)
    monkeypatch.setattr(service, '_notification_texts', AsyncMock(return_value=persian))
    monkeypatch.setattr(service, '_get_tariff_name', AsyncMock(return_value=None))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_send_message', send)
    user = SimpleNamespace(id=7, language='ru', balance_kopeks=50000, referred_by_id=None)
    subscription = SimpleNamespace(end_date=datetime.now(UTC) + timedelta(days=30))
    assert await service.send_subscription_update_notification(
        AsyncMock(), user, subscription, 'devices', 2, 3, price_paid=10000,
    )
    text = send.await_args.args[0]
    assert 'کاربر شمارهٔ 7' in text
    assert 'User#' not in text and 'Unknown' not in text
    assert '2' in text and '3' in text
    assert persian.format_price(10000) in text


@pytest.mark.asyncio
@pytest.mark.parametrize('has_user', [False, True])
async def test_withdrawal_reminder_identity_and_username(persian, monkeypatch, has_user):
    service = AdminNotificationService(SimpleNamespace())
    monkeypatch.setattr(service, '_is_enabled', lambda: True)
    monkeypatch.setattr(service, '_notification_texts', AsyncMock(return_value=persian))
    monkeypatch.setattr(service, 'resolve_recipient_role', lambda: 'admin')
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_send_message', send)
    user = SimpleNamespace(id=7, language='ru', username='example') if has_user else None
    request = SimpleNamespace(id=11, user_id=7, user=user, amount_kopeks=25000)
    assert await service.send_withdrawal_pending_reminder(request, 90)
    text = send.await_args.args[0]
    assert 'Unknown' not in text and 'Username' not in text and 'User#' not in text
    if has_user:
        assert 'نام کاربری' in text and 'https://t.me/example' in text
    else:
        assert 'نامشخص' in text
    assert persian.format_price(25000) in text


@pytest.mark.asyncio
@pytest.mark.parametrize('exists', [False, True])
async def test_missing_referrer_uses_persian_id(persian, monkeypatch, exists):
    service = AdminNotificationService(SimpleNamespace())
    referrer = SimpleNamespace(username=None, telegram_id=None, email=None) if exists else None
    monkeypatch.setattr('app.services.admin_notification_service.get_user_by_id', AsyncMock(return_value=referrer))
    expected = 'کاربر شمارهٔ 9' if exists else 'شناسهٔ 9 (یافت نشد)'
    assert await service._get_referrer_info(AsyncMock(), 9, texts=persian) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['username', 'telegram', 'email', 'failure'])
async def test_referrer_dynamic_fields(persian, monkeypatch, kind):
    service = AdminNotificationService(SimpleNamespace())
    referrer = SimpleNamespace(
        username='example' if kind == 'username' else None,
        telegram_id=123 if kind == 'telegram' else None,
        email='a&b@example.com' if kind == 'email' else None,
    )
    lookup = AsyncMock(return_value=referrer)
    if kind == 'failure':
        lookup.side_effect = RuntimeError('internal database details')
    monkeypatch.setattr('app.services.admin_notification_service.get_user_by_id', lookup)
    expected = {
        'username': '<a href="https://t.me/example">@example</a> (شناسه: 9)',
        'telegram': 'شناسهٔ 123',
        'email': '📧 a&amp;b@example.com',
        'failure': 'شناسهٔ 9 (خطا در دریافت اطلاعات)',
    }
    assert await service._get_referrer_info(AsyncMock(), 9, texts=persian) == expected[kind]


def test_all_payment_methods_have_display_names(persian):
    from app.database.models import PaymentMethod

    service = AdminNotificationService(SimpleNamespace())
    for method in PaymentMethod:
        label = service._get_payment_method_display(method.value, texts=persian)
        assert 'روش پرداخت ناشناخته' not in label, method
        assert label != f'💳 {method.value}', method
    assert service._get_payment_method_display(None, texts=persian) == persian.t('ADMIN_NOTIFY_025')
    assert service._get_payment_method_display('apple_iap', texts=persian) == '🍎 خرید درون‌برنامه‌ای اپل'
    assert 'پرداخت دوره‌ای' in service._get_payment_method_display('platega_recurrent', texts=persian)
    assert service._get_payment_method_display('<new_provider>', texts=persian) == (
        '💳 روش پرداخت ناشناخته (&lt;new_provider&gt;)'
    )


def test_added_provider_name_preserves_configuration(persian, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(type(settings), 'get_lava_display_name', lambda self: 'درگاه <ویژه>')
    service = AdminNotificationService(SimpleNamespace())
    assert service._get_payment_method_display('lava', texts=persian) == '💳 درگاه &lt;ویژه&gt;'
