"""Top-up presentation localizes referrer data at the recipient boundary."""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.admin_notification_service import AdminNotificationService
from app.utils.user_utils import format_referrer_info
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['username', 'id', 'missing', 'load_error', 'none'])
async def test_topup_localizes_referrer_and_manual_status(persian, monkeypatch, state):
    class LazyUser(SimpleNamespace):
        @property
        def referrer(self):
            raise RuntimeError('internal loading failure')

    fields = dict(
        id=1, telegram_id=123, language='ru', first_name='نام', username=None,
        balance_kopeks=50000, referred_by_id=None if state == 'none' else 7,
    )
    referrer = None if state == 'missing' else SimpleNamespace(
        username='example' if state == 'username' else None, telegram_id=456,
    )
    user = LazyUser(**fields) if state == 'load_error' else SimpleNamespace(**fields, referrer=referrer)
    # Existing adapters still prepare historical metadata before knowing the admin language.
    # Use that same input to verify it does not leak into the Persian message.
    original = 'ID 7 (ошибка загрузки)' if state == 'load_error' else 'ID 7 (не найден)'
    if state == 'none':
        original = 'Нет'
    transaction = SimpleNamespace(
        id=9, payment_method='manual', amount_kopeks=10000, external_id=None,
        description=None, created_at=None, completed_at=None, receipt_uuid=None,
    )
    service = AdminNotificationService(SimpleNamespace())
    monkeypatch.setattr(service, '_notification_texts', AsyncMock(return_value=persian))
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_record_subscription_event', record)
    monkeypatch.setattr(service, '_is_enabled', lambda: True)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_send_message', send)
    assert await service.send_balance_topup_notification(
        user, transaction, 40000, topup_status='🧾 Ручное пополнение', referrer_info=original,
        subscription=None, promo_group=None, db=AsyncMock(),
    )
    text = send.await_args.args[0]
    assert 'شارژ دستی' in text
    assert re.search('[А-Яа-яЁё]', text) is None
    assert 'internal loading failure' not in text
    expected = {
        'username': '<a href="https://t.me/example">@example</a> (شناسه: 7)',
        'id': 'شناسهٔ 456', 'missing': 'شناسهٔ 7 (یافت نشد)',
        'load_error': 'شناسهٔ 7 (خطا در دریافت اطلاعات)',
    }
    if state != 'none':
        assert expected[state] in text
    assert record.await_args.kwargs['extra']['referrer_info'] == original
    assert record.await_args.kwargs['amount_kopeks'] == 10000
    assert persian.format_price(10000) in text


def test_referrer_helper_preserves_html_escaping(persian):
    user = SimpleNamespace(referred_by_id=7, referrer=SimpleNamespace(username='<login>', telegram_id=None))
    assert format_referrer_info(user, texts=persian) == '@&lt;login&gt; (شناسه: 7)'
