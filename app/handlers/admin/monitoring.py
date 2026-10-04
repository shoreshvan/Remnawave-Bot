import asyncio
import html
from datetime import UTC, date, datetime, timedelta

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.keyboards.admin import get_monitoring_keyboard
from app.localization.texts import get_texts
from app.services.monitoring_service import monitoring_service
from app.services.nalogo_queue_service import nalogo_queue_service
from app.services.notification_settings_service import NotificationSettingsService
from app.services.traffic_monitoring_service import (
    traffic_monitoring_scheduler,
)
from app.states import AdminStates
from app.utils.decorators import admin_required
from app.utils.pagination import paginate_list
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)
router = Router()


def _format_toggle(enabled: bool) -> str:
    return '🟢 Вкл' if enabled else '🔴 Выкл'


def _build_notification_settings_view(language: str):
    texts = get_texts(language)
    config = NotificationSettingsService.get_config()

    second_percent = NotificationSettingsService.get_second_wave_discount_percent()
    second_hours = NotificationSettingsService.get_second_wave_valid_hours()
    third_percent = NotificationSettingsService.get_third_wave_discount_percent()
    third_hours = NotificationSettingsService.get_third_wave_valid_hours()
    third_days = NotificationSettingsService.get_third_wave_trigger_days()

    trial_channel_status = _format_toggle(config.get('trial_channel_unsubscribed', {}).get('enabled', True))
    expired_1d_status = _format_toggle(config['expired_1d'].get('enabled', True))
    second_wave_status = _format_toggle(config['expired_second_wave'].get('enabled', True))
    third_wave_status = _format_toggle(config['expired_third_wave'].get('enabled', True))

    summary_text = texts.t(
        'ADMIN_MON_NOTIFY_SUMMARY',
        '🔔 <b>Уведомления пользователям</b>\n\n'
        '• Отписка от канала: {trial_channel_status}\n'
        '• 1 день после истечения: {expired_1d_status}\n'
        '• 2-3 дня (скидка {second_percent}% / {second_hours} ч): {second_wave_status}\n'
        '• {third_days} дней (скидка {third_percent}% / {third_hours} ч): {third_wave_status}',
    ).format(
        trial_channel_status=trial_channel_status,
        expired_1d_status=expired_1d_status,
        second_percent=second_percent,
        second_hours=second_hours,
        second_wave_status=second_wave_status,
        third_days=third_days,
        third_percent=third_percent,
        third_hours=third_hours,
        third_wave_status=third_wave_status,
    )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MON_NOTIFY_BTN_TRIAL_CHANNEL', '{trial_channel_status} • Отписка от канала'
                    ).format(trial_channel_status=trial_channel_status),
                    callback_data='admin_mon_notify_toggle_trial_channel',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_BTN_TEST_TRIAL_CHANNEL', '🧪 Тест: отписка от канала'),
                    callback_data='admin_mon_notify_preview_trial_channel',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MON_NOTIFY_BTN_EXPIRED_1D', '{expired_1d_status} • 1 день после истечения'
                    ).format(expired_1d_status=expired_1d_status),
                    callback_data='admin_mon_notify_toggle_expired_1d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_BTN_TEST_EXPIRED_1D', '🧪 Тест: 1 день после истечения'),
                    callback_data='admin_mon_notify_preview_expired_1d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MON_NOTIFY_BTN_EXPIRED_2D', '{second_wave_status} • 2-3 дня со скидкой'
                    ).format(second_wave_status=second_wave_status),
                    callback_data='admin_mon_notify_toggle_expired_2d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_BTN_TEST_EXPIRED_2D', '🧪 Тест: скидка 2-3 день'),
                    callback_data='admin_mon_notify_preview_expired_2d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MON_NOTIFY_BTN_EDIT_2D_PERCENT', '✏️ Скидка 2-3 дня: {second_percent}%'
                    ).format(second_percent=second_percent),
                    callback_data='admin_mon_notify_edit_2d_percent',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MON_NOTIFY_BTN_EDIT_2D_HOURS', '⏱️ Срок скидки 2-3 дня: {second_hours} ч'
                    ).format(second_hours=second_hours),
                    callback_data='admin_mon_notify_edit_2d_hours',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MON_NOTIFY_BTN_EXPIRED_ND', '{third_wave_status} • {third_days} дней со скидкой'
                    ).format(third_wave_status=third_wave_status, third_days=third_days),
                    callback_data='admin_mon_notify_toggle_expired_nd',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_BTN_TEST_EXPIRED_ND', '🧪 Тест: скидка спустя дни'),
                    callback_data='admin_mon_notify_preview_expired_nd',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MON_NOTIFY_BTN_EDIT_ND_PERCENT', '✏️ Скидка {third_days} дней: {third_percent}%'
                    ).format(third_days=third_days, third_percent=third_percent),
                    callback_data='admin_mon_notify_edit_nd_percent',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MON_NOTIFY_BTN_EDIT_ND_HOURS', '⏱️ Срок скидки {third_days} дней: {third_hours} ч'
                    ).format(third_days=third_days, third_hours=third_hours),
                    callback_data='admin_mon_notify_edit_nd_hours',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MON_NOTIFY_BTN_EDIT_ND_THRESHOLD', '📆 Порог уведомления: {third_days} дн.'
                    ).format(third_days=third_days),
                    callback_data='admin_mon_notify_edit_nd_threshold',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_BTN_PREVIEW_ALL', '🧪 Отправить все тесты'),
                    callback_data='admin_mon_notify_preview_all',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'), callback_data='admin_mon_settings'
                )
            ],
        ]
    )

    return summary_text, keyboard


async def _build_notification_preview_message(language: str, notification_type: str):
    texts = get_texts(language)
    now = datetime.now(UTC)
    price_30_days = settings.format_price(settings.PRICE_30_DAYS)

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.keyboards.inline import get_channel_sub_keyboard
    from app.services.channel_subscription_service import channel_subscription_service

    header = texts.t('ADMIN_MON_PREVIEW_HEADER', '🧪 <b>Тестовое уведомление мониторинга</b>\n\n')

    if notification_type == 'trial_channel_unsubscribed':
        template = texts.get(
            'TRIAL_CHANNEL_UNSUBSCRIBED',
            (
                '🚫 <b>Доступ приостановлен</b>\n\n'
                'Мы не нашли вашу подписку на наш канал, поэтому тестовая подписка отключена.\n\n'
                'Подпишитесь на канал и нажмите «{check_button}», чтобы вернуть доступ.'
            ),
        )
        check_button = texts.t('CHANNEL_CHECK_BUTTON', '✅ Я подписался')
        message = template.format(check_button=check_button)
        # Use all required channels for the preview keyboard
        required_channels = await channel_subscription_service.get_required_channels()
        keyboard = get_channel_sub_keyboard(required_channels, language=language)
    elif notification_type == 'expired_1d':
        template = texts.get(
            'SUBSCRIPTION_EXPIRED_1D',
            (
                '⛔ <b>Подписка закончилась</b>\n\n'
                'Доступ был отключён {end_date}. Продлите подписку, чтобы вернуться в сервис.'
            ),
        )
        message = template.format(
            end_date=format_local_datetime(now - timedelta(days=1), '%d.%m.%Y %H:%M'),
            price=price_30_days,
            tariff_label='',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                        callback_data='subscription_extend',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'),
                        callback_data='balance_topup',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON', '🆘 Поддержка'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    elif notification_type == 'expired_2d':
        percent = NotificationSettingsService.get_second_wave_discount_percent()
        valid_hours = NotificationSettingsService.get_second_wave_valid_hours()
        template = texts.get(
            'SUBSCRIPTION_EXPIRED_SECOND_WAVE',
            (
                '🔥 <b>Скидка {percent}% на продление</b>\n\n'
                'Активируйте предложение, чтобы получить дополнительную скидку. '
                'Она суммируется с вашей промогруппой и действует до {expires_at}.'
            ),
        )
        message = template.format(
            percent=percent,
            expires_at=format_local_datetime(now + timedelta(hours=valid_hours), '%d.%m.%Y %H:%M'),
            trigger_days=3,
            tariff_label='',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_PREVIEW_BTN_CLAIM_DISCOUNT', '🎁 Получить скидку'),
                        callback_data='claim_discount_preview',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                        callback_data='subscription_extend',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'),
                        callback_data='balance_topup',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON', '🆘 Поддержка'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    elif notification_type == 'expired_nd':
        percent = NotificationSettingsService.get_third_wave_discount_percent()
        valid_hours = NotificationSettingsService.get_third_wave_valid_hours()
        trigger_days = NotificationSettingsService.get_third_wave_trigger_days()
        template = texts.get(
            'SUBSCRIPTION_EXPIRED_THIRD_WAVE',
            (
                '🎁 <b>Индивидуальная скидка {percent}%</b>\n\n'
                'Прошло {trigger_days} дней без подписки — возвращайтесь и активируйте дополнительную скидку. '
                'Она суммируется с промогруппой и действует до {expires_at}.'
            ),
        )
        message = template.format(
            percent=percent,
            trigger_days=trigger_days,
            expires_at=format_local_datetime(now + timedelta(hours=valid_hours), '%d.%m.%Y %H:%M'),
            tariff_label='',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_PREVIEW_BTN_CLAIM_DISCOUNT', '🎁 Получить скидку'),
                        callback_data='claim_discount_preview',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                        callback_data='subscription_extend',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'),
                        callback_data='balance_topup',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON', '🆘 Поддержка'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    else:
        raise ValueError(f'Unsupported notification type: {notification_type}')

    footer = texts.t(
        'ADMIN_MON_PREVIEW_FOOTER', '\n\n<i>Сообщение отправлено только вам для проверки оформления.</i>'
    )
    return header + message + footer, keyboard


async def _send_notification_preview(bot, chat_id: int, language: str, notification_type: str) -> None:
    message, keyboard = await _build_notification_preview_message(language, notification_type)
    await bot.send_message(
        chat_id,
        message,
        parse_mode='HTML',
        reply_markup=keyboard,
    )


async def _render_notification_settings(callback: CallbackQuery) -> None:
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    text, keyboard = _build_notification_settings_view(language)
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)


async def _render_notification_settings_for_state(
    bot,
    chat_id: int,
    message_id: int,
    language: str,
    business_connection_id: str | None = None,
) -> None:
    text, keyboard = _build_notification_settings_view(language)

    edit_kwargs = {
        'text': text,
        'chat_id': chat_id,
        'message_id': message_id,
        'parse_mode': 'HTML',
        'reply_markup': keyboard,
    }

    if business_connection_id:
        edit_kwargs['business_connection_id'] = business_connection_id

    try:
        await bot.edit_message_text(**edit_kwargs)
    except TelegramBadRequest as exc:
        if 'no text in the message to edit' in (exc.message or '').lower():
            caption_kwargs = {
                'chat_id': chat_id,
                'message_id': message_id,
                'caption': text,
                'parse_mode': 'HTML',
                'reply_markup': keyboard,
            }

            if business_connection_id:
                caption_kwargs['business_connection_id'] = business_connection_id

            await bot.edit_message_caption(**caption_kwargs)
        else:
            raise


@router.callback_query(F.data == 'admin_monitoring')
@admin_required
async def admin_monitoring_menu(callback: CallbackQuery):
    try:
        async with AsyncSessionLocal() as db:
            status = await monitoring_service.get_monitoring_status(db)

            texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
            running_status = (
                texts.t('ADMIN_MON_STATUS_RUNNING', '🟢 Работает')
                if status['is_running']
                else texts.t('ADMIN_MON_STATUS_STOPPED', '🔴 Остановлен')
            )
            last_update = (
                format_local_datetime(status['last_update'], '%H:%M:%S')
                if status['last_update']
                else texts.t('ADMIN_MON_LAST_UPDATE_NEVER', 'Никогда')
            )

            text = texts.t(
                'ADMIN_MON_MENU_TEXT',
                '\n'
                '🔍 <b>Система мониторинга</b>\n'
                '\n'
                '📊 <b>Статус:</b> {running_status}\n'
                '🕐 <b>Последнее обновление:</b> {last_update}\n'
                '⚙️ <b>Интервал проверки:</b> {interval} мин\n'
                '\n'
                '📈 <b>Статистика за 24 часа:</b>\n'
                '• Всего событий: {total_events}\n'
                '• Успешных: {successful}\n'
                '• Ошибок: {failed}\n'
                '• Успешность: {success_rate}%\n'
                '\n'
                '🔧 Выберите действие:\n',
            ).format(
                running_status=running_status,
                last_update=last_update,
                interval=settings.MONITORING_INTERVAL,
                total_events=status['stats_24h']['total_events'],
                successful=status['stats_24h']['successful'],
                failed=status['stats_24h']['failed'],
                success_rate=status['stats_24h']['success_rate'],
            )

            language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
            keyboard = get_monitoring_keyboard(language)
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в админ меню мониторинга', error=e)
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_ERROR_LOAD_DATA', '❌ Ошибка получения данных'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_settings')
@admin_required
async def admin_monitoring_settings(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        global_status = (
            texts.t('ADMIN_MON_NOTIFY_GLOBAL_ENABLED', '🟢 Включены')
            if NotificationSettingsService.are_notifications_globally_enabled()
            else texts.t('ADMIN_MON_NOTIFY_GLOBAL_DISABLED', '🔴 Отключены')
        )
        second_percent = NotificationSettingsService.get_second_wave_discount_percent()
        third_percent = NotificationSettingsService.get_third_wave_discount_percent()
        third_days = NotificationSettingsService.get_third_wave_trigger_days()

        text = texts.t(
            'ADMIN_MON_SETTINGS_TEXT',
            '⚙️ <b>Настройки мониторинга</b>\n\n'
            '🔔 <b>Уведомления пользователям:</b> {global_status}\n'
            '• Скидка 2-3 дня: {second_percent}%\n'
            '• Скидка после {third_days} дней: {third_percent}%\n\n'
            'Выберите раздел для настройки.',
        ).format(
            global_status=global_status,
            second_percent=second_percent,
            third_days=third_days,
            third_percent=third_percent,
        )

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_BTN_NOTIFY_SETTINGS', '🔔 Уведомления пользователям'),
                        callback_data='admin_mon_notify_settings',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'), callback_data='admin_submenu_settings'
                    )
                ],
            ]
        )

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка отображения настроек мониторинга', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_OPEN_SETTINGS', '❌ Не удалось открыть настройки'), show_alert=True
        )


@router.callback_query(F.data == 'admin_mon_notify_settings')
@admin_required
async def admin_notify_settings(callback: CallbackQuery):
    try:
        await _render_notification_settings(callback)
    except Exception as e:
        logger.error('Ошибка отображения настроек уведомлений', error=e)
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_LOAD_SETTINGS', '❌ Не удалось загрузить настройки'), show_alert=True
        )


@router.callback_query(F.data == 'admin_mon_notify_toggle_trial_channel')
@admin_required
async def toggle_trial_channel_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_trial_channel_unsubscribed_enabled()
    async with AsyncSessionLocal() as db:
        saved = await NotificationSettingsService.set_trial_channel_unsubscribed_enabled(db, not enabled)
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    if not saved:
        await callback.answer(
            texts.t('ADMIN_MON_SETTING_SAVE_FAILED', '❌ Не удалось сохранить настройку'), show_alert=True
        )
        return
    await callback.answer(
        texts.t('ADMIN_MON_TOAST_ENABLED', '✅ Включено')
        if not enabled
        else texts.t('ADMIN_MON_TOAST_DISABLED', '⏸️ Отключено')
    )
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_trial_channel')
@admin_required
async def preview_trial_channel_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'trial_channel_unsubscribed')
        await callback.answer(get_texts(language).t('ADMIN_MON_TOAST_PREVIEW_SENT', '✅ Пример отправлен'))
    except Exception as exc:
        logger.error('Failed to send trial channel preview', exc=exc)
        await callback.answer(
            get_texts(language).t('ADMIN_MON_TOAST_PREVIEW_FAILED', '❌ Не удалось отправить тест'),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_1d')
@admin_required
async def toggle_expired_1d_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_expired_1d_enabled()
    async with AsyncSessionLocal() as db:
        saved = await NotificationSettingsService.set_expired_1d_enabled(db, not enabled)
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    if not saved:
        await callback.answer(
            texts.t('ADMIN_MON_SETTING_SAVE_FAILED', '❌ Не удалось сохранить настройку'), show_alert=True
        )
        return
    await callback.answer(
        texts.t('ADMIN_MON_TOAST_ENABLED', '✅ Включено')
        if not enabled
        else texts.t('ADMIN_MON_TOAST_DISABLED', '⏸️ Отключено')
    )
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_1d')
@admin_required
async def preview_expired_1d_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_1d')
        await callback.answer(get_texts(language).t('ADMIN_MON_TOAST_PREVIEW_SENT', '✅ Пример отправлен'))
    except Exception as exc:
        logger.error('Failed to send expired 1d preview', exc=exc)
        await callback.answer(
            get_texts(language).t('ADMIN_MON_TOAST_PREVIEW_FAILED', '❌ Не удалось отправить тест'),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_2d')
@admin_required
async def toggle_second_wave_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_second_wave_enabled()
    async with AsyncSessionLocal() as db:
        saved = await NotificationSettingsService.set_second_wave_enabled(db, not enabled)
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    if not saved:
        await callback.answer(
            texts.t('ADMIN_MON_SETTING_SAVE_FAILED', '❌ Не удалось сохранить настройку'), show_alert=True
        )
        return
    await callback.answer(
        texts.t('ADMIN_MON_TOAST_ENABLED', '✅ Включено')
        if not enabled
        else texts.t('ADMIN_MON_TOAST_DISABLED', '⏸️ Отключено')
    )
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_2d')
@admin_required
async def preview_second_wave_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_2d')
        await callback.answer(get_texts(language).t('ADMIN_MON_TOAST_PREVIEW_SENT', '✅ Пример отправлен'))
    except Exception as exc:
        logger.error('Failed to send second wave preview', exc=exc)
        await callback.answer(
            get_texts(language).t('ADMIN_MON_TOAST_PREVIEW_FAILED', '❌ Не удалось отправить тест'),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_nd')
@admin_required
async def toggle_third_wave_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_third_wave_enabled()
    async with AsyncSessionLocal() as db:
        saved = await NotificationSettingsService.set_third_wave_enabled(db, not enabled)
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    if not saved:
        await callback.answer(
            texts.t('ADMIN_MON_SETTING_SAVE_FAILED', '❌ Не удалось сохранить настройку'), show_alert=True
        )
        return
    await callback.answer(
        texts.t('ADMIN_MON_TOAST_ENABLED', '✅ Включено')
        if not enabled
        else texts.t('ADMIN_MON_TOAST_DISABLED', '⏸️ Отключено')
    )
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_nd')
@admin_required
async def preview_third_wave_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_nd')
        await callback.answer(get_texts(language).t('ADMIN_MON_TOAST_PREVIEW_SENT', '✅ Пример отправлен'))
    except Exception as exc:
        logger.error('Failed to send third wave preview', exc=exc)
        await callback.answer(
            get_texts(language).t('ADMIN_MON_TOAST_PREVIEW_FAILED', '❌ Не удалось отправить тест'),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_notify_preview_all')
@admin_required
async def preview_all_notifications(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        chat_id = callback.from_user.id
        for notification_type in [
            'trial_channel_unsubscribed',
            'expired_1d',
            'expired_2d',
            'expired_nd',
        ]:
            await _send_notification_preview(callback.bot, chat_id, language, notification_type)
        await callback.answer(
            get_texts(language).t('ADMIN_MON_TOAST_ALL_PREVIEWS_SENT', '✅ Все тестовые уведомления отправлены')
        )
    except Exception as exc:
        logger.error('Failed to send all notification previews', exc=exc)
        await callback.answer(
            get_texts(language).t('ADMIN_MON_TOAST_ALL_PREVIEWS_FAILED', '❌ Не удалось отправить тесты'),
            show_alert=True,
        )


async def _start_notification_value_edit(
    callback: CallbackQuery,
    state: FSMContext,
    setting_key: str,
    field: str,
    prompt_key: str,
    default_prompt: str,
):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    await state.set_state(AdminStates.editing_notification_value)
    await state.update_data(
        notification_setting_key=setting_key,
        notification_setting_field=field,
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
        settings_business_connection_id=(
            str(getattr(callback.message, 'business_connection_id', None))
            if getattr(callback.message, 'business_connection_id', None) is not None
            else None
        ),
        settings_language=language,
    )
    texts = get_texts(language)
    await callback.answer()
    await callback.message.answer(texts.get(prompt_key, default_prompt))


@router.callback_query(F.data == 'admin_mon_notify_edit_2d_percent')
@admin_required
async def edit_second_wave_percent(callback: CallbackQuery, state: FSMContext):
    await _start_notification_value_edit(
        callback,
        state,
        'expired_second_wave',
        'percent',
        'NOTIFY_PROMPT_SECOND_PERCENT',
        'Введите новый процент скидки для уведомления через 2-3 дня (0-100):',
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_2d_hours')
@admin_required
async def edit_second_wave_hours(callback: CallbackQuery, state: FSMContext):
    await _start_notification_value_edit(
        callback,
        state,
        'expired_second_wave',
        'hours',
        'NOTIFY_PROMPT_SECOND_HOURS',
        'Введите количество часов действия скидки (1-168):',
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_percent')
@admin_required
async def edit_third_wave_percent(callback: CallbackQuery, state: FSMContext):
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'percent',
        'NOTIFY_PROMPT_THIRD_PERCENT',
        'Введите новый процент скидки для позднего предложения (0-100):',
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_hours')
@admin_required
async def edit_third_wave_hours(callback: CallbackQuery, state: FSMContext):
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'hours',
        'NOTIFY_PROMPT_THIRD_HOURS',
        'Введите количество часов действия скидки (1-168):',
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_threshold')
@admin_required
async def edit_third_wave_threshold(callback: CallbackQuery, state: FSMContext):
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'trigger',
        'NOTIFY_PROMPT_THIRD_DAYS',
        'Через сколько дней после истечения отправлять предложение? (минимум 2):',
    )


@router.callback_query(F.data == 'admin_mon_start')
@admin_required
async def start_monitoring_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        if monitoring_service.is_running:
            await callback.answer(texts.t('ADMIN_MON_TOAST_ALREADY_RUNNING', 'ℹ️ Мониторинг уже запущен'))
            return

        if not monitoring_service.bot:
            monitoring_service.bot = callback.bot

        asyncio.create_task(monitoring_service.start_monitoring())

        await callback.answer(texts.t('ADMIN_MON_TOAST_STARTED', '✅ Мониторинг запущен!'))

        await admin_monitoring_menu(callback)

    except Exception as e:
        logger.error('Ошибка запуска мониторинга', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_START', '❌ Ошибка запуска: {error!s}').format(error=e), show_alert=True
        )


@router.callback_query(F.data == 'admin_mon_stop')
@admin_required
async def stop_monitoring_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        if not monitoring_service.is_running:
            await callback.answer(texts.t('ADMIN_MON_TOAST_ALREADY_STOPPED', 'ℹ️ Мониторинг уже остановлен'))
            return

        monitoring_service.stop_monitoring()
        await callback.answer(texts.t('ADMIN_MON_TOAST_STOPPED', '⏹️ Мониторинг остановлен!'))

        await admin_monitoring_menu(callback)

    except Exception as e:
        logger.error('Ошибка остановки мониторинга', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_STOP', '❌ Ошибка остановки: {error!s}').format(error=e), show_alert=True
        )


@router.callback_query(F.data == 'admin_mon_force_check')
@admin_required
async def force_check_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_TOAST_CHECKING', '⏳ Выполняем проверку подписок...'))

        async with AsyncSessionLocal() as db:
            results = await monitoring_service.force_check_subscriptions(db)

            text = texts.t(
                'ADMIN_MON_FORCE_CHECK_RESULT',
                '\n'
                '✅ <b>Принудительная проверка завершена</b>\n'
                '\n'
                '📊 <b>Результаты проверки:</b>\n'
                '• Истекших подписок: {expired}\n'
                '• Истекающих подписок: {expiring}\n'
                '• Готовых к автооплате: {autopay_ready}\n'
                '\n'
                '🕐 <b>Время проверки:</b> {check_time}\n'
                '\n'
                'Нажмите "Назад" для возврата в меню мониторинга.\n',
            ).format(
                expired=results['expired'],
                expiring=results['expiring'],
                autopay_ready=results['autopay_ready'],
                check_time=format_local_datetime(datetime.now(UTC), '%H:%M:%S'),
            )

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'), callback_data='admin_monitoring'
                        )
                    ]
                ]
            )

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка принудительной проверки', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_CHECK', '❌ Ошибка проверки: {error!s}').format(error=e), show_alert=True
        )


@router.callback_query(F.data == 'admin_mon_traffic_check')
@admin_required
async def traffic_check_callback(callback: CallbackQuery):
    """Ручная проверка трафика — использует snapshot и дельту."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        # Проверяем, включен ли мониторинг трафика
        if not traffic_monitoring_scheduler.is_enabled():
            await callback.answer(
                texts.t(
                    'ADMIN_MON_TRAFFIC_DISABLED',
                    '⚠️ Мониторинг трафика отключен в настройках\nВключите TRAFFIC_FAST_CHECK_ENABLED=true в .env',
                ),
                show_alert=True,
            )
            return

        await callback.answer(texts.t('ADMIN_MON_TOAST_TRAFFIC_CHECKING', '⏳ Запускаем проверку трафика (дельта)...'))

        # Используем run_fast_check — он сравнивает с snapshot и отправляет уведомления
        from app.services.traffic_monitoring_service import traffic_monitoring_scheduler_v2

        # Устанавливаем бота, если не установлен
        if not traffic_monitoring_scheduler_v2.bot:
            traffic_monitoring_scheduler_v2.set_bot(callback.bot)

        violations = await traffic_monitoring_scheduler_v2.run_fast_check_now()

        # Получаем информацию о snapshot
        snapshot_age = await traffic_monitoring_scheduler_v2.service.get_snapshot_age_minutes()
        threshold_gb = traffic_monitoring_scheduler_v2.service.get_fast_check_threshold_gb()

        text = texts.t(
            'ADMIN_MON_TRAFFIC_CHECK_RESULT',
            '\n'
            '📊 <b>Проверка трафика завершена</b>\n'
            '\n'
            '🔍 <b>Результаты (дельта):</b>\n'
            '• Превышений за интервал: {violations_count}\n'
            '• Порог дельты: {threshold_gb} ГБ\n'
            '• Возраст snapshot: {snapshot_age:.1f} мин\n'
            '\n'
            '🕐 <b>Время проверки:</b> {check_time}\n',
        ).format(
            violations_count=len(violations),
            threshold_gb=threshold_gb,
            snapshot_age=snapshot_age,
            check_time=format_local_datetime(datetime.now(UTC), '%H:%M:%S'),
        )

        if violations:
            text += texts.t('ADMIN_MON_TRAFFIC_VIOLATIONS_HEADER', '\n⚠️ <b>Превышения дельты:</b>\n')
            for v in violations[:10]:
                # Числовой id панели не режется срезом, а при отсутствии идентичности
                # нельзя уронить весь экран в «❌ Ошибка» — показываем прочерк.
                fallback = (
                    texts.t('ADMIN_MON_TRAFFIC_USER_ID', 'ID {user_id}').format(user_id=v.user_id)
                    if v.user_id
                    else '—'
                )
                name = html.escape(v.full_name or '') or fallback
                text += texts.t(
                    'ADMIN_MON_TRAFFIC_VIOLATION_LINE', '• {name}: +{used_traffic_gb:.1f} ГБ\n'
                ).format(name=name, used_traffic_gb=v.used_traffic_gb)
            if len(violations) > 10:
                text += texts.t('ADMIN_MON_TRAFFIC_VIOLATIONS_MORE', '... и ещё {count}\n').format(
                    count=len(violations) - 10
                )
            text += texts.t('ADMIN_MON_TRAFFIC_NOTIFICATIONS_SENT', '\n📨 Уведомления отправлены (с учётом кулдауна)')
        else:
            text += texts.t('ADMIN_MON_TRAFFIC_NO_VIOLATIONS', '\n✅ Превышений не обнаружено')

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_BTN_RETRY', '🔄 Повторить'),
                        callback_data='admin_mon_traffic_check',
                    )
                ],
                [InlineKeyboardButton(text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'), callback_data='admin_monitoring')],
            ]
        )

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка проверки трафика', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_GENERIC', '❌ Ошибка: {error!s}').format(error=e), show_alert=True
        )


@router.callback_query(F.data.startswith('admin_mon_logs'))
@admin_required
async def monitoring_logs_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        page = 1
        if '_page_' in callback.data:
            page = int(callback.data.split('_page_')[1])

        async with AsyncSessionLocal() as db:
            all_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)

            if not all_logs:
                text = texts.t(
                    'ADMIN_MON_LOGS_EMPTY',
                    '📋 <b>Логи мониторинга пусты</b>\n\nСистема еще не выполнила проверки.',
                )
                keyboard = get_monitoring_logs_back_keyboard()
                await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
                return

            per_page = 8
            paginated_logs = paginate_list(all_logs, page=page, per_page=per_page)

            text = texts.t(
                'ADMIN_MON_LOGS_HEADER',
                '📋 <b>Логи мониторинга</b> (стр. {page}/{total_pages})\n\n',
            ).format(page=page, total_pages=paginated_logs.total_pages)

            for log in paginated_logs.items:
                icon = '✅' if log['is_success'] else '❌'
                time_str = format_local_datetime(log['created_at'], '%m-%d %H:%M')
                event_type = log['event_type'].replace('_', ' ').title()

                message = log['message']
                if len(message) > 45:
                    message = message[:45] + '...'

                text += f'{icon} <code>{time_str}</code> {event_type}\n'
                text += f'   📄 {message}\n\n'

            total_success = sum(1 for log in all_logs if log['is_success'])
            total_failed = len(all_logs) - total_success
            success_rate = round(total_success / len(all_logs) * 100, 1) if all_logs else 0

            text += texts.t('ADMIN_MON_LOGS_STATS_HEADER', '📊 <b>Общая статистика:</b>\n')
            text += texts.t('ADMIN_MON_LOGS_STATS_TOTAL', '• Всего событий: {count}\n').format(count=len(all_logs))
            text += texts.t('ADMIN_MON_LOGS_STATS_SUCCESS', '• Успешных: {count}\n').format(count=total_success)
            text += texts.t('ADMIN_MON_LOGS_STATS_FAILED', '• Ошибок: {count}\n').format(count=total_failed)
            text += texts.t('ADMIN_MON_LOGS_STATS_RATE', '• Успешность: {rate}%').format(rate=success_rate)

            keyboard = get_monitoring_logs_keyboard(page, paginated_logs.total_pages)
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка получения логов', error=e)
        await callback.answer(texts.t('ADMIN_MON_ERROR_GET_LOGS', '❌ Ошибка получения логов'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_clear_logs')
@admin_required
async def clear_logs_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        async with AsyncSessionLocal() as db:
            deleted_count = await monitoring_service.cleanup_old_logs(db, days=0)
            await db.commit()

            if deleted_count > 0:
                await callback.answer(
                    texts.t('ADMIN_MON_LOGS_DELETED', '🗑️ Удалено {count} записей логов').format(
                        count=deleted_count
                    )
                )
            else:
                await callback.answer(texts.t('ADMIN_MON_LOGS_ALREADY_EMPTY', 'ℹ️ Логи уже пусты'))

            await monitoring_logs_callback(callback)

    except Exception as e:
        logger.error('Ошибка очистки логов', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_CLEAR_LOGS', '❌ Ошибка очистки: {error!s}').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_test_notifications')
@admin_required
async def test_notifications_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        test_message = texts.t(
            'ADMIN_MON_TEST_MESSAGE',
            '\n🧪 <b>Тестовое уведомление системы мониторинга</b>\n\n'
            'Это тестовое сообщение для проверки работы системы уведомлений.\n\n'
            '📊 <b>Статус системы:</b>\n'
            '• Мониторинг: {monitoring_status}\n'
            '• Уведомления: {notifications_status}\n'
            '• Время теста: {test_time}\n\n'
            '✅ Если вы получили это сообщение, система уведомлений работает корректно!\n',
        ).format(
            monitoring_status=(
                texts.t('ADMIN_MON_TEST_MON_RUNNING', '🟢 Работает')
                if monitoring_service.is_running
                else texts.t('ADMIN_MON_TEST_MON_STOPPED', '🔴 Остановлен')
            ),
            notifications_status=(
                texts.t('ADMIN_MON_TEST_NOTIF_ENABLED', '🟢 Включены')
                if settings.ENABLE_NOTIFICATIONS
                else texts.t('ADMIN_MON_TEST_NOTIF_DISABLED', '🔴 Отключены')
            ),
            test_time=format_local_datetime(datetime.now(UTC), '%H:%M:%S %d.%m.%Y'),
        )

        await callback.bot.send_message(callback.from_user.id, test_message, parse_mode='HTML')

        await callback.answer(texts.t('ADMIN_MON_TEST_SENT', '✅ Тестовое уведомление отправлено!'))

    except Exception as e:
        logger.error('Ошибка отправки тестового уведомления', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_SEND', '❌ Ошибка отправки: {error!s}').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_statistics')
@admin_required
async def monitoring_statistics_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        async with AsyncSessionLocal() as db:
            from app.database.crud.subscription import get_subscriptions_statistics

            sub_stats = await get_subscriptions_statistics(db)

            mon_status = await monitoring_service.get_monitoring_status(db)

            week_ago = datetime.now(UTC) - timedelta(days=7)
            week_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)
            week_logs = [log for log in week_logs if log['created_at'] >= week_ago]

            week_success = sum(1 for log in week_logs if log['is_success'])
            week_errors = len(week_logs) - week_success

            text = texts.t(
                'ADMIN_MON_STATS_TEXT',
                '\n📊 <b>Статистика мониторинга</b>\n\n'
                '📱 <b>Подписки:</b>\n'
                '• Всего: {total_subs}\n'
                '• Активных: {active_subs}\n'
                '• Тестовых: {trial_subs}\n'
                '• Платных: {paid_subs}\n\n'
                '📈 <b>За сегодня:</b>\n'
                '• Успешных операций: {today_success}\n'
                '• Ошибок: {today_failed}\n'
                '• Успешность: {today_rate}%\n\n'
                '📊 <b>За неделю:</b>\n'
                '• Всего событий: {week_total}\n'
                '• Успешных: {week_success}\n'
                '• Ошибок: {week_failed}\n'
                '• Успешность: {week_rate}%\n\n'
                '🔧 <b>Система:</b>\n'
                '• Интервал: {interval} мин\n'
                '• Уведомления: {notifications_status}\n'
                '• Автооплата: {autopay_days} дней\n',
            ).format(
                total_subs=sub_stats['total_subscriptions'],
                active_subs=sub_stats['active_subscriptions'],
                trial_subs=sub_stats['trial_subscriptions'],
                paid_subs=sub_stats['paid_subscriptions'],
                today_success=mon_status['stats_24h']['successful'],
                today_failed=mon_status['stats_24h']['failed'],
                today_rate=mon_status['stats_24h']['success_rate'],
                week_total=len(week_logs),
                week_success=week_success,
                week_failed=week_errors,
                week_rate=round(week_success / len(week_logs) * 100, 1) if week_logs else 0,
                interval=settings.MONITORING_INTERVAL,
                notifications_status=(
                    texts.t('ADMIN_MON_STATS_NOTIF_ON', '🟢 Вкл')
                    if getattr(settings, 'ENABLE_NOTIFICATIONS', True)
                    else texts.t('ADMIN_MON_STATS_NOTIF_OFF', '🔴 Выкл')
                ),
                autopay_days=', '.join(map(str, settings.get_autopay_warning_days())),
            )

            # Добавляем информацию о чеках NaloGO
            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                queue_len = nalogo_status.get('queue_length', 0)
                total_amount = nalogo_status.get('total_amount', 0)
                running = nalogo_status.get('running', False)
                pending_count = nalogo_status.get('pending_verification_count', 0)
                pending_amount = nalogo_status.get('pending_verification_amount', 0)

                nalogo_section = texts.t(
                    'ADMIN_MON_STATS_NALOGO_SECTION',
                    '\n🧾 <b>Чеки NaloGO:</b>\n'
                    '• Сервис: {service_status}\n'
                    '• В очереди: {queue_len} чек(ов)',
                ).format(
                    service_status=(
                        texts.t('ADMIN_MON_STATS_NALOGO_RUNNING', '🟢 Работает')
                        if running
                        else texts.t('ADMIN_MON_STATS_NALOGO_STOPPED', '🔴 Остановлен')
                    ),
                    queue_len=queue_len,
                )
                if queue_len > 0:
                    nalogo_section += texts.t(
                        'ADMIN_MON_STATS_NALOGO_AMOUNT', '\n• На сумму: {amount:,.2f} تومان'
                    ).format(amount=total_amount)
                if pending_count > 0:
                    nalogo_section += texts.t(
                        'ADMIN_MON_STATS_NALOGO_PENDING',
                        '\n⚠️ <b>Требуют проверки: {count} ({amount:,.2f} تومان)</b>',
                    ).format(count=pending_count, amount=pending_amount)
                text += nalogo_section

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            buttons = []
            # Кнопки для работы с чеками NaloGO
            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                nalogo_buttons = []
                if nalogo_status.get('queue_length', 0) > 0:
                    nalogo_buttons.append(
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_MON_STATS_BTN_NALOGO_SEND', '🧾 Отправить ({count})').format(
                                count=nalogo_status['queue_length']
                            ),
                            callback_data='admin_mon_nalogo_force_process',
                        )
                    )
                pending_count = nalogo_status.get('pending_verification_count', 0)
                if pending_count > 0:
                    nalogo_buttons.append(
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_MON_STATS_BTN_NALOGO_CHECK', '⚠️ Проверить ({count})').format(
                                count=pending_count
                            ),
                            callback_data='admin_mon_nalogo_pending',
                        )
                    )
                nalogo_buttons.append(
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_STATS_BTN_RECONCILE', '📊 Сверка чеков'),
                        callback_data='admin_mon_receipts_missing',
                    )
                )
                buttons.append(nalogo_buttons)

            buttons.append(
                [InlineKeyboardButton(text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'), callback_data='admin_monitoring')]
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка получения статистики', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_STATS_ERROR', '❌ Ошибка получения статистики: {error!s}').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_nalogo_force_process')
@admin_required
async def nalogo_force_process_callback(callback: CallbackQuery):
    """Принудительная отправка чеков из очереди."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(
            texts.t('ADMIN_MON_NALOGO_PROCESSING', '🔄 Запускаю обработку очереди чеков...'), show_alert=False
        )

        result = await nalogo_queue_service.force_process()

        if 'error' in result:
            await callback.answer(f'❌ {result["error"]}', show_alert=True)
            return

        result.get('message', 'Готово')
        processed = result.get('processed', 0)
        remaining = result.get('remaining', 0)

        if processed > 0:
            text = texts.t('ADMIN_MON_NALOGO_PROCESSED', '✅ Обработано: {count} чек(ов)').format(count=processed)
            if remaining > 0:
                text += texts.t('ADMIN_MON_NALOGO_REMAINING', '\n⏳ Осталось в очереди: {count}').format(
                    count=remaining
                )
        elif remaining > 0:
            text = texts.t(
                'ADMIN_MON_NALOGO_UNAVAILABLE',
                '⚠️ Сервис nalog.ru недоступен\n⏳ В очереди: {count} чек(ов)',
            ).format(count=remaining)
        else:
            text = texts.t('ADMIN_MON_NALOGO_QUEUE_EMPTY', '📭 Очередь пуста')

        await callback.answer(text, show_alert=True)

        # Обновляем страницу статистики
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        # Перезагружаем статистику
        async with AsyncSessionLocal() as db:
            from app.database.crud.subscription import get_subscriptions_statistics

            sub_stats = await get_subscriptions_statistics(db)
            mon_status = await monitoring_service.get_monitoring_status(db)

            week_ago = datetime.now(UTC) - timedelta(days=7)
            week_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)
            week_logs = [log for log in week_logs if log['created_at'] >= week_ago]
            week_success = sum(1 for log in week_logs if log['is_success'])
            week_errors = len(week_logs) - week_success

            stats_text = texts.t(
                'ADMIN_MON_STATS_TEXT',
                '\n📊 <b>Статистика мониторинга</b>\n\n'
                '📱 <b>Подписки:</b>\n'
                '• Всего: {total_subs}\n'
                '• Активных: {active_subs}\n'
                '• Тестовых: {trial_subs}\n'
                '• Платных: {paid_subs}\n\n'
                '📈 <b>За сегодня:</b>\n'
                '• Успешных операций: {today_success}\n'
                '• Ошибок: {today_failed}\n'
                '• Успешность: {today_rate}%\n\n'
                '📊 <b>За неделю:</b>\n'
                '• Всего событий: {week_total}\n'
                '• Успешных: {week_success}\n'
                '• Ошибок: {week_failed}\n'
                '• Успешность: {week_rate}%\n\n'
                '🔧 <b>Система:</b>\n'
                '• Интервал: {interval} мин\n'
                '• Уведомления: {notifications_status}\n'
                '• Автооплата: {autopay_days} дней\n',
            ).format(
                total_subs=sub_stats['total_subscriptions'],
                active_subs=sub_stats['active_subscriptions'],
                trial_subs=sub_stats['trial_subscriptions'],
                paid_subs=sub_stats['paid_subscriptions'],
                today_success=mon_status['stats_24h']['successful'],
                today_failed=mon_status['stats_24h']['failed'],
                today_rate=mon_status['stats_24h']['success_rate'],
                week_total=len(week_logs),
                week_success=week_success,
                week_failed=week_errors,
                week_rate=round(week_success / len(week_logs) * 100, 1) if week_logs else 0,
                interval=settings.MONITORING_INTERVAL,
                notifications_status=(
                    texts.t('ADMIN_MON_STATS_NOTIF_ON', '🟢 Вкл')
                    if getattr(settings, 'ENABLE_NOTIFICATIONS', True)
                    else texts.t('ADMIN_MON_STATS_NOTIF_OFF', '🔴 Выкл')
                ),
                autopay_days=', '.join(map(str, settings.get_autopay_warning_days())),
            )

            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                queue_len = nalogo_status.get('queue_length', 0)
                total_amount = nalogo_status.get('total_amount', 0)
                running = nalogo_status.get('running', False)

                nalogo_section = texts.t(
                    'ADMIN_MON_STATS_NALOGO_SECTION',
                    '\n🧾 <b>Чеки NaloGO:</b>\n'
                    '• Сервис: {service_status}\n'
                    '• В очереди: {queue_len} чек(ов)',
                ).format(
                    service_status=(
                        texts.t('ADMIN_MON_STATS_NALOGO_RUNNING', '🟢 Работает')
                        if running
                        else texts.t('ADMIN_MON_STATS_NALOGO_STOPPED', '🔴 Остановлен')
                    ),
                    queue_len=queue_len,
                )
                if queue_len > 0:
                    nalogo_section += texts.t(
                        'ADMIN_MON_STATS_NALOGO_AMOUNT', '\n• На сумму: {amount:,.2f} تومان'
                    ).format(amount=total_amount)
                stats_text += nalogo_section

            buttons = []
            # Кнопки для работы с чеками NaloGO
            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                nalogo_buttons = []
                if nalogo_status.get('queue_length', 0) > 0:
                    nalogo_buttons.append(
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_MON_STATS_BTN_NALOGO_SEND', '🧾 Отправить ({count})').format(
                                count=nalogo_status['queue_length']
                            ),
                            callback_data='admin_mon_nalogo_force_process',
                        )
                    )
                nalogo_buttons.append(
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_STATS_BTN_RECONCILE', '📊 Сверка чеков'),
                        callback_data='admin_mon_receipts_missing',
                    )
                )
                buttons.append(nalogo_buttons)

            buttons.append(
                [InlineKeyboardButton(text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'), callback_data='admin_monitoring')]
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            await callback.message.edit_text(stats_text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка принудительной обработки чеков', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_GENERIC', '❌ Ошибка: {error!s}').format(error=e), show_alert=True
        )


@router.callback_query(F.data == 'admin_mon_nalogo_pending')
@admin_required
async def nalogo_pending_callback(callback: CallbackQuery):
    """Просмотр чеков ожидающих ручной проверки."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from app.services.nalogo_service import NaloGoService

        nalogo_service = NaloGoService()
        receipts = await nalogo_service.get_pending_verification_receipts()

        if not receipts:
            await callback.answer(texts.t('ADMIN_MON_NALOGO_NO_PENDING', '✅ Нет чеков на проверку'), show_alert=True)
            return

        text = texts.t('ADMIN_MON_NALOGO_PENDING_HEADER', '⚠️ <b>Чеки требующие проверки: {count}</b>\n\n').format(
            count=len(receipts)
        )
        text += texts.t(
            'ADMIN_MON_NALOGO_PENDING_HINT', 'Проверьте в lknpd.nalog.ru созданы ли эти чеки.\n\n'
        )

        buttons = []
        for i, receipt in enumerate(receipts[:10], 1):
            payment_id = receipt.get('payment_id', 'unknown')
            amount = receipt.get('amount', 0)
            created_at = receipt.get('created_at', '')[:16].replace('T', ' ')
            error = receipt.get('error', '')[:50]

            text += f'<b>{i}. {amount:,.2f} تومان</b>\n'
            text += f'   📅 {created_at}\n'
            text += f'   🆔 <code>{payment_id[:20]}...</code>\n'
            if error:
                text += f'   ❌ {error}\n'
            text += '\n'

            # Кнопки для каждого чека
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_NALOGO_BTN_CREATED', '✅ Создан ({index})').format(index=i),
                        callback_data=f'admin_nalogo_verified:{payment_id[:30]}',
                    ),
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_NALOGO_BTN_RETRY', '🔄 Отправить ({index})').format(index=i),
                        callback_data=f'admin_nalogo_retry:{payment_id[:30]}',
                    ),
                ]
            )

        if len(receipts) > 10:
            text += texts.t('ADMIN_MON_NALOGO_PENDING_MORE', '\n... и ещё {count} чек(ов)').format(
                count=len(receipts) - 10
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NALOGO_BTN_CLEAR_ALL', '🗑 Очистить всё (проверено)'),
                    callback_data='admin_nalogo_clear_pending',
                )
            ]
        )
        buttons.append(
            [InlineKeyboardButton(text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'), callback_data='admin_mon_statistics')]
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка просмотра очереди проверки', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_GENERIC', '❌ Ошибка: {error!s}').format(error=e), show_alert=True
        )


@router.callback_query(F.data.startswith('admin_nalogo_verified:'))
@admin_required
async def nalogo_mark_verified_callback(callback: CallbackQuery):
    """Пометить чек как созданный в налоговой."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        from app.services.nalogo_service import NaloGoService

        payment_id = callback.data.split(':', 1)[1]
        nalogo_service = NaloGoService()

        # Помечаем как проверенный (чек был создан)
        removed = await nalogo_service.mark_pending_as_verified(payment_id, receipt_uuid=None, was_created=True)

        if removed:
            await callback.answer(
                texts.t('ADMIN_MON_NALOGO_MARKED_CREATED', '✅ Чек помечен как созданный'), show_alert=True
            )
            # Обновляем список
            await nalogo_pending_callback(callback)
        else:
            await callback.answer(texts.t('ADMIN_MON_NALOGO_NOT_FOUND', '❌ Чек не найден'), show_alert=True)

    except Exception as e:
        logger.error('Ошибка пометки чека', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_GENERIC', '❌ Ошибка: {error!s}').format(error=e), show_alert=True
        )


@router.callback_query(F.data.startswith('admin_nalogo_retry:'))
@admin_required
async def nalogo_retry_callback(callback: CallbackQuery):
    """Повторно отправить чек в налоговую."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        from app.services.nalogo_service import NaloGoService

        payment_id = callback.data.split(':', 1)[1]
        nalogo_service = NaloGoService()

        await callback.answer(texts.t('ADMIN_MON_NALOGO_RETRY_SENDING', '🔄 Отправляю чек...'), show_alert=False)

        # bot обязателен: без него чек уйдёт в ФНС, но покупатель его не получит
        receipt_uuid = await nalogo_service.retry_pending_receipt(payment_id, bot=callback.bot)

        if receipt_uuid:
            await callback.answer(
                texts.t('ADMIN_MON_NALOGO_RECEIPT_CREATED', '✅ Чек создан: {uuid}').format(uuid=receipt_uuid),
                show_alert=True,
            )
            # Обновляем список
            await nalogo_pending_callback(callback)
        else:
            await callback.answer(
                texts.t('ADMIN_MON_NALOGO_RECEIPT_FAILED', '❌ Не удалось создать чек'), show_alert=True
            )

    except Exception as e:
        logger.error('Ошибка повторной отправки чека', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_GENERIC', '❌ Ошибка: {error!s}').format(error=e), show_alert=True
        )


@router.callback_query(F.data == 'admin_nalogo_clear_pending')
@admin_required
async def nalogo_clear_pending_callback(callback: CallbackQuery):
    """Очистить всю очередь проверки."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        from app.services.nalogo_service import NaloGoService

        nalogo_service = NaloGoService()
        count = await nalogo_service.clear_pending_verification()

        await callback.answer(
            texts.t('ADMIN_MON_NALOGO_CLEARED', '✅ Очищено: {count} чек(ов)').format(count=count), show_alert=True
        )
        # Возвращаемся на статистику
        await callback.message.edit_text(
            texts.t('ADMIN_MON_NALOGO_QUEUE_CLEARED', '✅ Очередь проверки очищена'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'),
                            callback_data='admin_mon_statistics',
                        )
                    ]
                ]
            ),
        )

    except Exception as e:
        logger.error('Ошибка очистки очереди', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_GENERIC', '❌ Ошибка: {error!s}').format(error=e), show_alert=True
        )


@router.callback_query(F.data == 'admin_mon_receipts_missing')
@admin_required
async def receipts_missing_callback(callback: CallbackQuery):
    """Сверка чеков по логам."""
    # Напрямую вызываем сверку по логам
    await _do_reconcile_logs(callback)


@router.callback_query(F.data == 'admin_mon_receipts_link_old')
@admin_required
async def receipts_link_old_callback(callback: CallbackQuery):
    """Привязать старые чеки из NaloGO к транзакциям по сумме и дате."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        from sqlalchemy import and_, select

        from app.database.models import PaymentMethod, Transaction, TransactionType
        from app.services.nalogo_service import NaloGoService

        await callback.answer(texts.t('ADMIN_MON_LINK_LOADING', '🔄 Загружаю чеки из NaloGO...'), show_alert=False)

        TRACKING_START_DATE = datetime(2024, 12, 29, 0, 0, 0, tzinfo=UTC)

        async with AsyncSessionLocal() as db:
            # Получаем старые транзакции без чеков
            query = (
                select(Transaction)
                .where(
                    and_(
                        Transaction.type == TransactionType.DEPOSIT.value,
                        Transaction.payment_method == PaymentMethod.YOOKASSA.value,
                        Transaction.receipt_uuid.is_(None),
                        Transaction.is_completed == True,
                        Transaction.created_at < TRACKING_START_DATE,
                    )
                )
                .order_by(Transaction.created_at.desc())
            )

            result = await db.execute(query)
            transactions = result.scalars().all()

            if not transactions:
                await callback.answer(
                    texts.t('ADMIN_MON_LINK_NO_OLD_TX', '✅ Нет старых транзакций для привязки'), show_alert=True
                )
                return

            # Получаем чеки из NaloGO за последние 60 дней
            nalogo_service = NaloGoService()
            to_date = date.today()
            from_date = to_date - timedelta(days=60)

            incomes = await nalogo_service.get_incomes(
                from_date=from_date,
                to_date=to_date,
                limit=500,
            )

            if not incomes:
                await callback.answer(
                    texts.t('ADMIN_MON_LINK_NO_INCOMES', '❌ Не удалось получить чеки из NaloGO'), show_alert=True
                )
                return

            # Создаём словарь чеков по сумме для быстрого поиска
            # Ключ: сумма в копейках, значение: список чеков
            incomes_by_amount = {}
            for income in incomes:
                amount = float(income.get('totalAmount', income.get('amount', 0)))
                amount_kopeks = int(amount * 100)
                if amount_kopeks not in incomes_by_amount:
                    incomes_by_amount[amount_kopeks] = []
                incomes_by_amount[amount_kopeks].append(income)

            linked = 0
            for t in transactions:
                if t.amount_kopeks in incomes_by_amount:
                    matching_incomes = incomes_by_amount[t.amount_kopeks]
                    if matching_incomes:
                        # Берём первый подходящий чек
                        income = matching_incomes.pop(0)
                        receipt_uuid = income.get('approvedReceiptUuid', income.get('receiptUuid'))
                        if receipt_uuid:
                            t.receipt_uuid = receipt_uuid
                            # Парсим дату чека
                            operation_time = income.get('operationTime')
                            if operation_time:
                                try:
                                    from dateutil.parser import isoparse

                                    parsed_time = isoparse(operation_time)
                                    t.receipt_created_at = (
                                        parsed_time if parsed_time.tzinfo else parsed_time.replace(tzinfo=UTC)
                                    )
                                except Exception:
                                    t.receipt_created_at = datetime.now(UTC)
                            linked += 1

            if linked > 0:
                await db.commit()

            text = texts.t('ADMIN_MON_LINK_DONE_HEADER', '🔗 <b>Привязка завершена</b>\n\n')
            text += texts.t('ADMIN_MON_LINK_TOTAL_TX', 'Всего транзакций: {count}\n').format(count=len(transactions))
            text += texts.t('ADMIN_MON_LINK_INCOMES_COUNT', 'Чеков в NaloGO: {count}\n').format(count=len(incomes))
            text += texts.t('ADMIN_MON_LINK_LINKED', 'Привязано: <b>{count}</b>\n').format(count=linked)
            text += texts.t('ADMIN_MON_LINK_FAILED', 'Не удалось привязать: {count}').format(
                count=len(transactions) - linked
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'),
                            callback_data='admin_mon_statistics',
                        )
                    ],
                ]
            )

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка привязки старых чеков', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_GENERIC', '❌ Ошибка: {error!s}').format(error=e), show_alert=True
        )


@router.callback_query(F.data == 'admin_mon_receipts_reconcile')
@admin_required
async def receipts_reconcile_menu_callback(callback: CallbackQuery, state: FSMContext):
    """Меню выбора периода сверки."""

    # Очищаем состояние на случай если остался ввод даты
    await state.clear()

    # Сразу показываем сверку по логам
    await _do_reconcile_logs(callback)


async def _do_reconcile_logs(callback: CallbackQuery):
    """Внутренняя функция сверки по логам."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        import re
        from collections import defaultdict
        from pathlib import Path

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        await callback.answer(
            texts.t('ADMIN_MON_RECONCILE_ANALYZING', '🔄 Анализирую логи платежей...'), show_alert=False
        )

        # Путь к файлу логов платежей (logs/current/)
        log_file_path = await asyncio.to_thread(Path(settings.LOG_FILE).resolve)
        log_dir = log_file_path.parent
        current_dir = log_dir / 'current'
        payments_log = current_dir / settings.LOG_PAYMENTS_FILE

        if not await asyncio.to_thread(payments_log.exists):
            try:
                await callback.message.edit_text(
                    texts.t(
                        'ADMIN_MON_RECONCILE_LOG_NOT_FOUND',
                        '❌ <b>Файл логов не найден</b>\n\n'
                        'Путь: <code>{path}</code>\n\n'
                        '<i>Логи появятся после первого успешного платежа.</i>',
                    ).format(path=payments_log),
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=texts.t('ADMIN_MON_RECONCILE_BTN_REFRESH', '🔄 Обновить'),
                                    callback_data='admin_mon_reconcile_logs',
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'),
                                    callback_data='admin_mon_statistics',
                                )
                            ],
                        ]
                    ),
                )
            except TelegramBadRequest:
                pass  # Сообщение не изменилось
            return

        # Паттерны для парсинга логов
        # Успешный платёж: "Успешно обработан платеж YooKassa 30e3c6fc-000f-5001-9000-1a9c8b242396: пользователь 1046 пополнил баланс на 200.0₽"
        payment_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}.*Успешно обработан платеж YooKassa ([a-f0-9-]+).*на ([\d.]+) تومان'
        )
        # Чек создан: "Чек NaloGO создан для платежа 30e3c6fc-000f-5001-9000-1a9c8b242396: 243udsqtik"
        receipt_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}.*Чек NaloGO создан для платежа ([a-f0-9-]+): (\w+)'
        )

        # Читаем и парсим логи
        payments = {}  # payment_id -> {date, amount}
        receipts = {}  # payment_id -> {date, receipt_uuid}

        try:
            with open(payments_log, encoding='utf-8') as f:
                for line in f:
                    # Проверяем платежи
                    match = payment_pattern.search(line)
                    if match:
                        date_str, payment_id, amount = match.groups()
                        payments[payment_id] = {'date': date_str, 'amount': float(amount)}
                        continue

                    # Проверяем чеки
                    match = receipt_pattern.search(line)
                    if match:
                        date_str, payment_id, receipt_uuid = match.groups()
                        receipts[payment_id] = {'date': date_str, 'receipt_uuid': receipt_uuid}
        except Exception as e:
            logger.error('Ошибка чтения логов', error=e)
            await callback.message.edit_text(
                texts.t('ADMIN_MON_RECONCILE_READ_ERROR', '❌ <b>Ошибка чтения логов</b>\n\n{error!s}').format(
                    error=e
                ),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'),
                                callback_data='admin_mon_statistics',
                            )
                        ]
                    ]
                ),
            )
            return

        # Находим платежи без чеков
        payments_without_receipts = []
        for payment_id, payment_data in payments.items():
            if payment_id not in receipts:
                payments_without_receipts.append(
                    {'payment_id': payment_id, 'date': payment_data['date'], 'amount': payment_data['amount']}
                )

        # Группируем по датам
        by_date = defaultdict(list)
        for p in payments_without_receipts:
            by_date[p['date']].append(p)

        # Формируем отчёт
        total_payments = len(payments)
        total_receipts = len(receipts)
        missing_count = len(payments_without_receipts)
        missing_amount = sum(p['amount'] for p in payments_without_receipts)

        text = texts.t('ADMIN_MON_RECONCILE_REPORT_HEADER', '📋 <b>Сверка по логам</b>\n\n')
        text += texts.t('ADMIN_MON_RECONCILE_TOTAL_PAYMENTS', '📦 <b>Всего платежей:</b> {count}\n').format(
            count=total_payments
        )
        text += texts.t('ADMIN_MON_RECONCILE_TOTAL_RECEIPTS', '🧾 <b>Чеков создано:</b> {count}\n\n').format(
            count=total_receipts
        )

        if missing_count == 0:
            text += texts.t('ADMIN_MON_RECONCILE_ALL_HAVE_RECEIPTS', '✅ <b>Все платежи имеют чеки!</b>')
        else:
            text += texts.t(
                'ADMIN_MON_RECONCILE_MISSING',
                '⚠️ <b>Без чеков:</b> {count} платежей на {amount:,.2f} تومان\n\n',
            ).format(count=missing_count, amount=missing_amount)

            # Показываем по датам (последние)
            sorted_dates = sorted(by_date.keys(), reverse=True)
            for date_str in sorted_dates[:7]:
                date_payments = by_date[date_str]
                date_amount = sum(p['amount'] for p in date_payments)
                text += texts.t(
                    'ADMIN_MON_RECONCILE_DATE_LINE', '• <b>{date}:</b> {count} шт. на {amount:,.2f} تومان\n'
                ).format(date=date_str, count=len(date_payments), amount=date_amount)

            if len(sorted_dates) > 7:
                text += texts.t('ADMIN_MON_RECONCILE_MORE_DAYS', '\n<i>...и ещё {count} дней</i>').format(
                    count=len(sorted_dates) - 7
                )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_RECONCILE_BTN_REFRESH', '🔄 Обновить'),
                        callback_data='admin_mon_reconcile_logs',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_RECONCILE_BTN_DETAILS', '📄 Детали'),
                        callback_data='admin_mon_reconcile_logs_details',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'),
                        callback_data='admin_mon_statistics',
                    )
                ],
            ]
        )

        try:
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest:
            pass  # Сообщение не изменилось

    except TelegramBadRequest:
        pass  # Игнорируем если сообщение не изменилось
    except Exception as e:
        logger.error('Ошибка сверки по логам', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_GENERIC', '❌ Ошибка: {error!s}').format(error=e), show_alert=True
        )


@router.callback_query(F.data == 'admin_mon_reconcile_logs')
@admin_required
async def receipts_reconcile_logs_refresh_callback(callback: CallbackQuery):
    """Обновить сверку по логам."""
    await _do_reconcile_logs(callback)


@router.callback_query(F.data == 'admin_mon_reconcile_logs_details')
@admin_required
async def receipts_reconcile_logs_details_callback(callback: CallbackQuery):
    """Детальный список платежей без чеков."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        import re
        from pathlib import Path

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        await callback.answer(texts.t('ADMIN_MON_DETAILS_LOADING', '🔄 Загружаю детали...'), show_alert=False)

        # Путь к логам (logs/current/)
        log_file_path = await asyncio.to_thread(Path(settings.LOG_FILE).resolve)
        log_dir = log_file_path.parent
        current_dir = log_dir / 'current'
        payments_log = current_dir / settings.LOG_PAYMENTS_FILE

        if not await asyncio.to_thread(payments_log.exists):
            await callback.answer(
                texts.t('ADMIN_MON_DETAILS_LOG_NOT_FOUND', '❌ Файл логов не найден'), show_alert=True
            )
            return

        payment_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}).*Успешно обработан платеж YooKassa ([a-f0-9-]+).*пользователь (\d+).*на ([\d.]+) تومان'
        )
        receipt_pattern = re.compile(r'Чек NaloGO создан для платежа ([a-f0-9-]+)')

        payments = {}
        receipts = set()

        with open(payments_log, encoding='utf-8') as f:
            for line in f:
                match = payment_pattern.search(line)
                if match:
                    date_str, time_str, payment_id, user_id, amount = match.groups()
                    payments[payment_id] = {
                        'date': date_str,
                        'time': time_str,
                        'user_id': user_id,
                        'amount': float(amount),
                    }
                    continue

                match = receipt_pattern.search(line)
                if match:
                    receipts.add(match.group(1))

        # Платежи без чеков
        missing = []
        for payment_id, data in payments.items():
            if payment_id not in receipts:
                missing.append({'payment_id': payment_id, **data})

        # Сортируем по дате (новые сверху)
        missing.sort(key=lambda x: (x['date'], x['time']), reverse=True)

        if not missing:
            text = texts.t('ADMIN_MON_RECONCILE_ALL_HAVE_RECEIPTS', '✅ <b>Все платежи имеют чеки!</b>')
        else:
            text = texts.t('ADMIN_MON_DETAILS_HEADER', '📄 <b>Платежи без чеков ({count} шт.)</b>\n\n').format(
                count=len(missing)
            )

            for p in missing[:20]:
                text += texts.t(
                    'ADMIN_MON_DETAILS_PAYMENT_LINE',
                    '• <b>{date} {time}</b>\n'
                    '  User: {user_id} | {amount:.0f} تومان\n'
                    '  <code>{payment_id}...</code>\n\n',
                ).format(
                    date=p['date'],
                    time=p['time'],
                    user_id=p['user_id'],
                    amount=p['amount'],
                    payment_id=p['payment_id'][:18],
                )

            if len(missing) > 20:
                text += texts.t('ADMIN_MON_DETAILS_MORE', '<i>...и ещё {count} платежей</i>').format(
                    count=len(missing) - 20
                )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_BACK', '⬅️ Назад'),
                        callback_data='admin_mon_reconcile_logs',
                    )
                ],
            ]
        )

        try:
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest:
            pass

    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.error('Ошибка детализации', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_MON_ERROR_GENERIC', '❌ Ошибка: {error!s}').format(error=e), show_alert=True
        )


def get_monitoring_logs_keyboard(current_page: int, total_pages: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = []

    if total_pages > 1:
        nav_row = []

        if current_page > 1:
            nav_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'admin_mon_logs_page_{current_page - 1}'))

        nav_row.append(InlineKeyboardButton(text=f'{current_page}/{total_pages}', callback_data='current_page'))

        if current_page < total_pages:
            nav_row.append(InlineKeyboardButton(text='➡️', callback_data=f'admin_mon_logs_page_{current_page + 1}'))

        keyboard.append(nav_row)

    keyboard.extend(
        [
            [
                InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_mon_logs'),
                InlineKeyboardButton(text='🗑️ Очистить', callback_data='admin_mon_clear_logs'),
            ],
            [InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_monitoring')],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_monitoring_logs_back_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_mon_logs'),
                InlineKeyboardButton(text='🔍 Фильтры', callback_data='admin_mon_logs_filters'),
            ],
            [InlineKeyboardButton(text='🗑️ Очистить логи', callback_data='admin_mon_clear_logs')],
            [InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_monitoring')],
        ]
    )


@router.message(Command('monitoring'))
@admin_required
async def monitoring_command(message: Message):
    try:
        texts = get_texts(message.from_user.language_code or settings.DEFAULT_LANGUAGE)
        async with AsyncSessionLocal() as db:
            status = await monitoring_service.get_monitoring_status(db)

            running_status = (
                texts.t('ADMIN_MON_STATUS_RUNNING', '🟢 Работает')
                if status['is_running']
                else texts.t('ADMIN_MON_STATUS_STOPPED', '🔴 Остановлен')
            )

            text = texts.t(
                'ADMIN_MON_COMMAND_STATUS_TEXT',
                '\n'
                '🔍 <b>Быстрый статус мониторинга</b>\n'
                '\n'
                '📊 <b>Статус:</b> {status}\n'
                '📈 <b>События за 24ч:</b> {events}\n'
                '✅ <b>Успешность:</b> {rate}%\n'
                '\n'
                'Для подробного управления используйте админ-панель.\n',
            ).format(
                status=running_status,
                events=status['stats_24h']['total_events'],
                rate=status['stats_24h']['success_rate'],
            )

            await message.answer(text, parse_mode='HTML')

    except Exception as e:
        logger.error('Ошибка команды /monitoring', error=e)
        await message.answer(texts.t('ADMIN_MON_ERROR_GENERIC', '❌ Ошибка: {error!s}').format(error=e))


@router.message(AdminStates.editing_notification_value)
async def process_notification_value_input(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data:
        await state.clear()
        texts = get_texts(message.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await message.answer(
            texts.t('ADMIN_MON_CONTEXT_LOST', 'ℹ️ Контекст утерян, попробуйте снова из меню настроек.')
        )
        return

    raw_value = (message.text or '').strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        language = data.get('settings_language') or message.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await message.answer(texts.get('NOTIFICATION_VALUE_INVALID', '❌ Введите целое число.'))
        return

    key = data.get('notification_setting_key')
    field = data.get('notification_setting_field')
    language = data.get('settings_language') or message.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)

    # Добавляем дополнительные проверки диапазона значений
    if (key == 'expired_second_wave' and field == 'percent') or (key == 'expired_third_wave' and field == 'percent'):
        if value < 0 or value > 100:
            await message.answer(
                texts.t('ADMIN_MON_NOTIFY_PERCENT_RANGE', '❌ Процент скидки должен быть от 0 до 100.')
            )
            return
    elif (key == 'expired_second_wave' and field == 'hours') or (key == 'expired_third_wave' and field == 'hours'):
        if value < 1 or value > 168:  # Максимум 168 часов (7 дней)
            await message.answer(
                texts.t('ADMIN_MON_NOTIFY_HOURS_RANGE', '❌ Количество часов должно быть от 1 до 168.')
            )
            return
    elif key == 'expired_third_wave' and field == 'trigger':
        if value < 2:  # Минимум 2 дня
            await message.answer(
                texts.t('ADMIN_MON_NOTIFY_DAYS_MIN', '❌ Количество дней должно быть не менее 2.')
            )
            return

    setters = {
        ('expired_second_wave', 'percent'): NotificationSettingsService.set_second_wave_discount_percent,
        ('expired_second_wave', 'hours'): NotificationSettingsService.set_second_wave_valid_hours,
        ('expired_third_wave', 'percent'): NotificationSettingsService.set_third_wave_discount_percent,
        ('expired_third_wave', 'hours'): NotificationSettingsService.set_third_wave_valid_hours,
        ('expired_third_wave', 'trigger'): NotificationSettingsService.set_third_wave_trigger_days,
    }
    setter = setters.get((key, field))
    success = False
    if setter is not None:
        async with AsyncSessionLocal() as db:
            success = await setter(db, value)

    if not success:
        await message.answer(texts.get('NOTIFICATION_VALUE_INVALID', '❌ Некорректное значение, попробуйте снова.'))
        return

    back_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.get('BACK', '⬅️ Назад'),
                    callback_data='admin_mon_notify_settings',
                )
            ]
        ]
    )

    await message.answer(
        texts.get('NOTIFICATION_VALUE_UPDATED', '✅ Настройки обновлены.'),
        reply_markup=back_keyboard,
    )

    chat_id = data.get('settings_message_chat')
    message_id = data.get('settings_message_id')
    business_connection_id = data.get('settings_business_connection_id')
    if chat_id and message_id:
        await _render_notification_settings_for_state(
            message.bot,
            chat_id,
            message_id,
            language,
            business_connection_id=business_connection_id,
        )

    await state.clear()


# ============== Настройки мониторинга трафика ==============


def _format_traffic_toggle(enabled: bool) -> str:
    return '🟢 Вкл' if enabled else '🔴 Выкл'


def _build_traffic_settings_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру настроек мониторинга трафика."""
    fast_enabled = settings.TRAFFIC_FAST_CHECK_ENABLED
    daily_enabled = settings.TRAFFIC_DAILY_CHECK_ENABLED

    fast_interval = settings.TRAFFIC_FAST_CHECK_INTERVAL_MINUTES
    fast_threshold = settings.TRAFFIC_FAST_CHECK_THRESHOLD_GB
    daily_time = settings.TRAFFIC_DAILY_CHECK_TIME
    daily_threshold = settings.TRAFFIC_DAILY_THRESHOLD_GB
    cooldown = settings.TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f'{_format_traffic_toggle(fast_enabled)} Быстрая проверка',
                    callback_data='admin_traffic_toggle_fast',
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'⏱ Интервал: {fast_interval} мин', callback_data='admin_traffic_edit_fast_interval'
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'📊 Порог дельты: {fast_threshold} ГБ', callback_data='admin_traffic_edit_fast_threshold'
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'{_format_traffic_toggle(daily_enabled)} Суточная проверка',
                    callback_data='admin_traffic_toggle_daily',
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'🕐 Время проверки: {daily_time}', callback_data='admin_traffic_edit_daily_time'
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'📈 Суточный порог: {daily_threshold} ГБ', callback_data='admin_traffic_edit_daily_threshold'
                )
            ],
            [InlineKeyboardButton(text=f'⏳ Кулдаун: {cooldown} мин', callback_data='admin_traffic_edit_cooldown')],
            [InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_monitoring')],
        ]
    )


def _build_traffic_settings_text() -> str:
    """Строит текст настроек мониторинга трафика."""
    fast_enabled = settings.TRAFFIC_FAST_CHECK_ENABLED
    daily_enabled = settings.TRAFFIC_DAILY_CHECK_ENABLED

    fast_status = _format_traffic_toggle(fast_enabled)
    daily_status = _format_traffic_toggle(daily_enabled)

    text = (
        '⚙️ <b>Настройки мониторинга трафика</b>\n\n'
        f'<b>Быстрая проверка:</b> {fast_status}\n'
        f'• Интервал: {settings.TRAFFIC_FAST_CHECK_INTERVAL_MINUTES} мин\n'
        f'• Порог дельты: {settings.TRAFFIC_FAST_CHECK_THRESHOLD_GB} ГБ\n\n'
        f'<b>Суточная проверка:</b> {daily_status}\n'
        f'• Время: {settings.TRAFFIC_DAILY_CHECK_TIME} UTC\n'
        f'• Порог: {settings.TRAFFIC_DAILY_THRESHOLD_GB} ГБ\n\n'
        f'<b>Общие:</b>\n'
        f'• Кулдаун уведомлений: {settings.TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES} мин\n'
    )

    # Информация о фильтрах
    monitored_nodes = settings.get_traffic_monitored_nodes()
    ignored_nodes = settings.get_traffic_ignored_nodes()
    excluded_user_ids = settings.get_traffic_excluded_user_ids()

    if monitored_nodes:
        text += f'• Мониторим только: {len(monitored_nodes)} нод(ы)\n'
    if ignored_nodes:
        text += f'• Игнорируем: {len(ignored_nodes)} нод(ы)\n'
    if excluded_user_ids:
        text += f'• Исключено юзеров: {len(excluded_user_ids)}\n'

    return text


@router.callback_query(F.data == 'admin_mon_traffic_settings')
@admin_required
async def admin_traffic_settings(callback: CallbackQuery):
    """Показывает настройки мониторинга трафика."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        text = _build_traffic_settings_text()
        keyboard = _build_traffic_settings_keyboard()
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    except Exception as e:
        logger.error('Ошибка отображения настроек трафика', error=e)
        await callback.answer(
            texts.t('ADMIN_MON_TRAFFIC_SETTINGS_LOAD_ERROR', '❌ Ошибка загрузки настроек'), show_alert=True
        )


@router.callback_query(F.data == 'admin_traffic_toggle_fast')
@admin_required
async def toggle_fast_check(callback: CallbackQuery):
    """Переключает быструю проверку трафика."""
    try:
        from app.services.system_settings_service import BotConfigurationService

        current = settings.TRAFFIC_FAST_CHECK_ENABLED
        new_value = not current

        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, 'TRAFFIC_FAST_CHECK_ENABLED', new_value)
            await db.commit()

        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(
            texts.t('ADMIN_MON_TOAST_ENABLED', '✅ Включено')
            if new_value
            else texts.t('ADMIN_MON_TOAST_DISABLED', '⏸️ Отключено')
        )

        # Обновляем отображение
        text = _build_traffic_settings_text()
        keyboard = _build_traffic_settings_keyboard()
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка переключения быстрой проверки', error=e)
        await callback.answer(texts.t('ADMIN_MON_ERROR_SHORT', '❌ Ошибка'), show_alert=True)


@router.callback_query(F.data == 'admin_traffic_toggle_daily')
@admin_required
async def toggle_daily_check(callback: CallbackQuery):
    """Переключает суточную проверку трафика."""
    try:
        from app.services.system_settings_service import BotConfigurationService

        current = settings.TRAFFIC_DAILY_CHECK_ENABLED
        new_value = not current

        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, 'TRAFFIC_DAILY_CHECK_ENABLED', new_value)
            await db.commit()

        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(
            texts.t('ADMIN_MON_TOAST_ENABLED', '✅ Включено')
            if new_value
            else texts.t('ADMIN_MON_TOAST_DISABLED', '⏸️ Отключено')
        )

        text = _build_traffic_settings_text()
        keyboard = _build_traffic_settings_keyboard()
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка переключения суточной проверки', error=e)
        await callback.answer(texts.t('ADMIN_MON_ERROR_SHORT', '❌ Ошибка'), show_alert=True)


@router.callback_query(F.data == 'admin_traffic_edit_fast_interval')
@admin_required
async def edit_fast_interval(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование интервала быстрой проверки."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_FAST_CHECK_INTERVAL_MINUTES',
        traffic_setting_type='int',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.message.answer(
        texts.t('ADMIN_MON_TRAFFIC_PROMPT_FAST_INTERVAL', '⏱ Введите интервал быстрой проверки в минутах (минимум 1):')
    )


@router.callback_query(F.data == 'admin_traffic_edit_fast_threshold')
@admin_required
async def edit_fast_threshold(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование порога быстрой проверки."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_FAST_CHECK_THRESHOLD_GB',
        traffic_setting_type='float',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.message.answer(
        texts.t('ADMIN_MON_TRAFFIC_PROMPT_FAST_THRESHOLD', '📊 Введите порог дельты трафика в ГБ (например: 5.0):')
    )


@router.callback_query(F.data == 'admin_traffic_edit_daily_time')
@admin_required
async def edit_daily_time(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование времени суточной проверки."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_DAILY_CHECK_TIME',
        traffic_setting_type='time',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.message.answer(
        texts.t(
            'ADMIN_MON_TRAFFIC_PROMPT_DAILY_TIME',
            '🕐 Введите время суточной проверки в формате HH:MM (в часовом поясе бота):\nНапример: 00:00, 03:00, 12:30',
        )
    )


@router.callback_query(F.data == 'admin_traffic_edit_daily_threshold')
@admin_required
async def edit_daily_threshold(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование суточного порога."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_DAILY_THRESHOLD_GB',
        traffic_setting_type='float',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.message.answer(
        texts.t('ADMIN_MON_TRAFFIC_PROMPT_DAILY_THRESHOLD', '📈 Введите суточный порог трафика в ГБ (например: 50.0):')
    )


@router.callback_query(F.data == 'admin_traffic_edit_cooldown')
@admin_required
async def edit_cooldown(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование кулдауна уведомлений."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES',
        traffic_setting_type='int',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.message.answer(
        texts.t('ADMIN_MON_TRAFFIC_PROMPT_COOLDOWN', '⏳ Введите кулдаун уведомлений в минутах (минимум 1):')
    )


@router.message(AdminStates.editing_traffic_setting)
async def process_traffic_setting_input(message: Message, state: FSMContext):
    """Обрабатывает ввод настройки мониторинга трафика."""
    from app.services.system_settings_service import BotConfigurationService

    data = await state.get_data()
    if not data:
        await state.clear()
        texts = get_texts(message.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await message.answer(
            texts.t('ADMIN_MON_CONTEXT_LOST', 'ℹ️ Контекст утерян, попробуйте снова из меню настроек.')
        )
        return

    raw_value = (message.text or '').strip()
    setting_key = data.get('traffic_setting_key')
    setting_type = data.get('traffic_setting_type')
    texts = get_texts(message.from_user.language_code or settings.DEFAULT_LANGUAGE)

    # Валидация и парсинг значения
    try:
        if setting_type == 'int':
            value = int(raw_value)
            if value < 1:
                raise ValueError(texts.t('ADMIN_MON_TRAFFIC_VAL_MIN_1', 'Значение должно быть >= 1'))
        elif setting_type == 'float':
            value = float(raw_value.replace(',', '.'))
            if value <= 0:
                raise ValueError(texts.t('ADMIN_MON_TRAFFIC_VAL_GT_0', 'Значение должно быть > 0'))
        elif setting_type == 'time':
            # Валидация формата HH:MM
            import re

            if not re.match(r'^\d{1,2}:\d{2}$', raw_value):
                raise ValueError(
                    texts.t('ADMIN_MON_TRAFFIC_VAL_TIME_FORMAT', 'Неверный формат времени. Используйте HH:MM')
                )
            parts = raw_value.split(':')
            hours, minutes = int(parts[0]), int(parts[1])
            if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
                raise ValueError(texts.t('ADMIN_MON_TRAFFIC_VAL_TIME_INVALID', 'Неверное время'))
            value = f'{hours:02d}:{minutes:02d}'
        else:
            value = raw_value
    except ValueError as e:
        await message.answer(f'❌ {e!s}')
        return

    # Сохраняем значение
    try:
        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, setting_key, value)
            await db.commit()

        back_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_TRAFFIC_BACK_TO_SETTINGS', '⬅️ К настройкам трафика'),
                        callback_data='admin_mon_traffic_settings',
                    )
                ]
            ]
        )
        await message.answer(
            texts.t('ADMIN_MON_TRAFFIC_SETTING_SAVED', '✅ Настройка сохранена!'), reply_markup=back_keyboard
        )

        # Обновляем исходное сообщение с настройками
        chat_id = data.get('settings_message_chat')
        message_id = data.get('settings_message_id')
        if chat_id and message_id:
            try:
                text = _build_traffic_settings_text()
                keyboard = _build_traffic_settings_keyboard()
                await message.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text, parse_mode='HTML', reply_markup=keyboard
                )
            except Exception:
                pass  # Игнорируем если сообщение уже удалено

    except Exception as e:
        logger.error('Ошибка сохранения настройки трафика', error=e)
        await message.answer(
            texts.t('ADMIN_MON_TRAFFIC_SAVE_ERROR', '❌ Ошибка сохранения: {error!s}').format(error=e)
        )

    await state.clear()


def register_handlers(dp):
    dp.include_router(router)
