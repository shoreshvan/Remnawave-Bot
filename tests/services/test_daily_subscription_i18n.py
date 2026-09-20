"""Daily notification translations and unchanged Russian fallback output."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.localization import texts as localization
from app.services import daily_subscription_service as module


@pytest.mark.parametrize('language', ['fa', 'ru'])
@pytest.mark.parametrize('multi_tariff', [False, True])
async def test_daily_notifications_localized(monkeypatch, language, multi_tariff):
    locale_dir = Path(__file__).resolve().parents[2] / 'app' / 'localization' / 'locales'
    monkeypatch.setattr(
        localization, 'load_locale',
        lambda lang: json.loads((locale_dir / f'{lang}.json').read_text(encoding='utf-8')),
    )
    monkeypatch.setattr(type(module.settings), 'is_multi_tariff_enabled', lambda self: multi_tariff)
    delivery = SimpleNamespace(notify_daily_debit=AsyncMock(), send_notification=AsyncMock())
    monkeypatch.setattr(module, 'notification_delivery_service', delivery)
    service = module.DailySubscriptionService()
    user = SimpleNamespace(language=language, balance_kopeks=12345)
    subscription = SimpleNamespace(tariff=SimpleNamespace(name='Test'), traffic_limit_gb=100)

    await service._notify_daily_charge(user, subscription, 1250)
    await service._notify_insufficient_balance(user, subscription, 15000)
    await service._notify_traffic_reset(user, subscription, 50)

    delivery.notify_daily_debit.assert_awaited_once()
    assert delivery.send_notification.await_count == 2
    charge = delivery.notify_daily_debit.await_args.kwargs
    insufficient, reset = [call.kwargs for call in delivery.send_notification.await_args_list]
    name = ' «Test»' if multi_tariff else ''
    if language == 'fa':
        label = '\n📦 تعرفه: «Test»' if multi_tariff else ''
        expected_charge = (
            '💳 <b>برداشت روزانه</b>\n\nکسر شده: 12.50 ₽\n'
            f'مانده موجودی: 123.45 ₽{label}\n\nبرداشت بعدی ۲۴ ساعت دیگر انجام می‌شود.'
        )
        expected_insufficient = (
            f'⚠️ <b>اشتراک{name} به حالت تعلیق درآمده است</b>\n\n'
            'موجودی برای پرداخت روزانه کافی نیست.\n\n'
            'مبلغ موردنیاز: 150.00 ₽\nموجودی: 123.45 ₽\n\n'
            'برای ازسرگیری اشتراک، موجودی خود را شارژ کنید.'
        )
        expected_reset = (
            'ℹ️ <b>حذف ترافیک اضافه</b>\n\n'
            'ترافیک اضافه خریداری‌شده شما (50 گیگ) به دلیل گذشت ۳۰ روز از اولین خرید ترافیک اضافه '
            f'حذف شد.{label}\n\nسقف فعلی ترافیک: 100 گیگ\n\n'
            'هر زمان بخواهید می‌توانید دوباره ترافیک اضافه بخرید.'
        )
        buttons = ['💳 شارژ موجودی', '📱 اشتراک من']
    else:
        label = '\n📦 Тариф: «Test»' if multi_tariff else ''
        expected_charge = (
            '💳 <b>Суточное списание</b>\n\nСписано: 12.50 ₽\n'
            f'Остаток баланса: 123.45 ₽{label}\n\nСледующее списание через 24 часа.'
        )
        expected_insufficient = (
            f'⚠️ <b>Подписка{name} приостановлена</b>\n\n'
            'Недостаточно средств для суточной оплаты.\n\n'
            'Требуется: 150.00 ₽\nБаланс: 123.45 ₽\n\n'
            'Пополните баланс, чтобы возобновить подписку.'
        )
        expected_reset = (
            'ℹ️ <b>Сброс докупленного трафика</b>\n\n'
            'Ваш докупленный трафик (50 ГБ) был сброшен, '
            f'так как прошло 30 дней с момента первой докупки.{label}\n\n'
            'Текущий лимит трафика: 100 ГБ\n\n'
            'Вы можете докупить трафик снова в любое время.'
        )
        buttons = ['💳 Пополнить баланс', '📱 Моя подписка']

    assert charge['telegram_message'] == expected_charge
    assert insufficient['telegram_message'] == expected_insufficient
    assert reset['telegram_message'] == expected_reset
    keyboard = insufficient['telegram_markup'].inline_keyboard
    assert [row[0].text for row in keyboard] == buttons
    assert [row[0].callback_data for row in keyboard] == ['menu_balance', 'menu_subscription']
    assert charge['amount_kopeks'] == 1250
    assert charge['new_balance_kopeks'] == 12345
    assert insufficient['context'] == {'required_amount': '150.00 ₽', 'current_balance': '123.45 ₽'}
    assert reset['context'] == {'reset_gb': 50, 'current_limit_gb': 100}
    assert insufficient['notification_type'] == module.NotificationType.DAILY_INSUFFICIENT_FUNDS
    assert reset['notification_type'] == module.NotificationType.TRAFFIC_RESET
    for call in (charge, insufficient, reset):
        assert call['user'] is user
        assert call['bot'] is service._bot
        assert '???' not in call['telegram_message']
        assert '\ufffd' not in call['telegram_message']
