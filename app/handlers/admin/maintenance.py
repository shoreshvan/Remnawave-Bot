import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.admin import get_admin_main_keyboard, get_maintenance_keyboard
from app.localization.texts import get_texts
from app.services.maintenance_service import maintenance_service
from app.services.system_settings_service import bot_configuration_service
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


class MaintenanceStates(StatesGroup):
    waiting_for_reason = State()
    waiting_for_notification_message = State()


# Дописывается к ответу, когда MAINTENANCE_MODE задан в .env. Такой ключ попадает
# в ENV_OVERRIDE_KEYS: переключение ложится в БД, но к настройкам не применяется, и
# на старте set_bot читает значение из окружения. Без этого предупреждения админ
# видит «выключено», а после перезапуска техработы включаются снова — и выглядит
# это как самопроизвольное включение.
_ENV_LOCKED_WARNING = (
    '\n\n⚠️ MAINTENANCE_MODE задан в .env, поэтому переключение не переживёт '
    'перезапуск. Уберите строку из .env, чтобы управлять режимом отсюда.'
)


def _maintenance_env_locked() -> bool:
    return bot_configuration_service.is_env_overridden('MAINTENANCE_MODE')


async def _persist_maintenance_mode(db: AsyncSession, enabled: bool) -> None:
    try:
        await bot_configuration_service.set_value(db, 'MAINTENANCE_MODE', enabled)
        await db.commit()
    except Exception as error:
        logger.error('Не удалось сохранить MAINTENANCE_MODE после переключения из бота', enabled=enabled, error=error)


@admin_required
@error_handler
async def show_maintenance_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)

    status_info = maintenance_service.get_status_info()

    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()
        panel_status = await rw_service.get_panel_status_summary()
    except Exception as e:
        logger.error('Ошибка получения статуса панели', error=e)
        panel_status = {
            'description': texts.t('MAINTENANCE_PANEL_STATUS_UNAVAILABLE', '❓ Не удалось проверить'),
            'has_issues': True,
        }

    status_emoji = '🔧' if status_info['is_active'] else '✅'
    status_text = (
        texts.t('MAINTENANCE_MODE_ON', 'Включен')
        if status_info['is_active']
        else texts.t('MAINTENANCE_MODE_OFF', 'Выключен')
    )

    api_emoji = '✅' if status_info['api_status'] else '❌'
    api_text = (
        texts.t('MAINTENANCE_API_AVAILABLE', 'Доступно')
        if status_info['api_status']
        else texts.t('MAINTENANCE_API_UNAVAILABLE', 'Недоступно')
    )

    monitoring_emoji = '🔄' if status_info['monitoring_active'] else '⏹️'
    monitoring_text = (
        texts.t('MAINTENANCE_MONITORING_RUNNING', 'Запущен')
        if status_info['monitoring_active']
        else texts.t('MAINTENANCE_MONITORING_STOPPED', 'Остановлен')
    )

    enabled_info = ''
    if status_info['is_active'] and status_info['enabled_at']:
        enabled_time = status_info['enabled_at'].strftime('%d.%m.%Y %H:%M:%S')
        enabled_info = texts.t('MAINTENANCE_ENABLED_AT', '\n📅 <b>Включен:</b> {enabled_time}').format(
            enabled_time=enabled_time
        )
        if status_info['reason']:
            enabled_info += texts.t('MAINTENANCE_REASON_LINE', '\n📝 <b>Причина:</b> {reason}').format(
                reason=status_info['reason']
            )

    last_check_info = ''
    if status_info['last_check']:
        last_check_time = status_info['last_check'].strftime('%H:%M:%S')
        last_check_info = texts.t(
            'MAINTENANCE_LAST_CHECK', '\n🕐 <b>Последняя проверка:</b> {last_check_time}'
        ).format(last_check_time=last_check_time)

    failures_info = ''
    if status_info['consecutive_failures'] > 0:
        failures_info = texts.t(
            'MAINTENANCE_CONSECUTIVE_FAILURES', '\n⚠️ <b>Неудачных проверок подряд:</b> {failures}'
        ).format(failures=status_info['consecutive_failures'])

    panel_info = texts.t('MAINTENANCE_PANEL_LINE', '\n🌐 <b>Панель Remnawave:</b> {description}').format(
        description=panel_status['description']
    )
    if panel_status.get('response_time'):
        panel_info += texts.t(
            'MAINTENANCE_PANEL_RESPONSE_TIME', '\n⚡ <b>Время отклика:</b> {response_time}с'
        ).format(response_time=panel_status['response_time'])

    message_text = texts.t(
        'MAINTENANCE_PANEL_TEXT',
        """
🔧 <b>Управление техническими работами</b>

{status_emoji} <b>Режим техработ:</b> {status_text}
{api_emoji} <b>API Remnawave:</b> {api_text}
{monitoring_emoji} <b>Мониторинг:</b> {monitoring_text}
🛠️ <b>Автозапуск мониторинга:</b> {monitoring_autostart}
⏱️ <b>Интервал проверки:</b> {check_interval}с
🤖 <b>Автовключение:</b> {auto_enable}
{panel_info}
{enabled_info}
{last_check_info}
{failures_info}

ℹ️ <i>В режиме техработ обычные пользователи не могут использовать бота. Администраторы имеют полный доступ.</i>
""",
    ).format(
        status_emoji=status_emoji,
        status_text=status_text,
        api_emoji=api_emoji,
        api_text=api_text,
        monitoring_emoji=monitoring_emoji,
        monitoring_text=monitoring_text,
        monitoring_autostart=(
            texts.t('MAINTENANCE_AUTOSTART_ON', 'Включен')
            if status_info['monitoring_configured']
            else texts.t('MAINTENANCE_AUTOSTART_OFF', 'Отключен')
        ),
        check_interval=status_info['check_interval'],
        auto_enable=(
            texts.t('MAINTENANCE_AUTOENABLE_ON', 'Включено')
            if status_info['auto_enable_configured']
            else texts.t('MAINTENANCE_AUTOENABLE_OFF', 'Отключено')
        ),
        panel_info=panel_info,
        enabled_info=enabled_info,
        last_check_info=last_check_info,
        failures_info=failures_info,
    )

    await callback.message.edit_text(
        message_text,
        reply_markup=get_maintenance_keyboard(
            db_user.language,
            status_info['is_active'],
            status_info['monitoring_active'],
            panel_status.get('has_issues', False),
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_maintenance_mode(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    is_active = maintenance_service.is_maintenance_active()

    if is_active:
        success = await maintenance_service.disable_maintenance()
        if success:
            await _persist_maintenance_mode(db, False)
        if success:
            text = texts.t('MAINTENANCE_MODE_DISABLED', 'Режим техработ выключен')
            if _maintenance_env_locked():
                text += _ENV_LOCKED_WARNING
            await callback.answer(text, show_alert=True)
        else:
            await callback.answer(
                texts.t('MAINTENANCE_MODE_DISABLE_ERROR', 'Ошибка выключения режима техработ'),
                show_alert=True,
            )
    else:
        await state.set_state(MaintenanceStates.waiting_for_reason)
        await callback.message.edit_text(
            texts.t(
                'MAINTENANCE_ENABLE_PROMPT',
                '🔧 <b>Включение режима техработ</b>\n\n'
                'Введите причину включения техработ или отправьте /skip для пропуска:',
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='maintenance_panel'
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_maintenance_reason(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    current_state = await state.get_state()

    if current_state != MaintenanceStates.waiting_for_reason:
        return

    reason = None
    if message.text and message.text != '/skip':
        reason = message.text[:200]

    success = await maintenance_service.enable_maintenance(reason=reason, auto=False)
    if success:
        await _persist_maintenance_mode(db, True)

    if success:
        response_text = texts.t('MAINTENANCE_MODE_ENABLED', 'Режим техработ включен')
        if reason:
            response_text += texts.t('MAINTENANCE_ENABLED_REASON', '\nПричина: {reason}').format(
                reason=html.escape(reason)
            )
        if _maintenance_env_locked():
            response_text += _ENV_LOCKED_WARNING
    else:
        response_text = texts.t('MAINTENANCE_MODE_ENABLE_ERROR', 'Ошибка включения режима техработ')

    await message.answer(response_text)
    await state.clear()

    maintenance_service.get_status_info()
    await message.answer(
        texts.t('MAINTENANCE_RETURN_PROMPT', 'Вернуться к панели управления техработами:'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[
                types.InlineKeyboardButton(
                    text=texts.t('MAINTENANCE_PANEL_BUTTON', '🔧 Панель техработ'), callback_data='maintenance_panel'
                )
            ]]
        ),
    )


@admin_required
@error_handler
async def toggle_monitoring(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    status_info = maintenance_service.get_status_info()

    if status_info['monitoring_active']:
        success = await maintenance_service.stop_monitoring()
        message = (
            texts.t('MAINTENANCE_MONITORING_STOPPED_ALERT', 'Мониторинг остановлен')
            if success
            else texts.t('MAINTENANCE_MONITORING_STOP_ERROR', 'Ошибка остановки мониторинга')
        )
    else:
        success = await maintenance_service.start_monitoring()
        message = (
            texts.t('MAINTENANCE_MONITORING_STARTED_ALERT', 'Мониторинг запущен')
            if success
            else texts.t('MAINTENANCE_MONITORING_START_ERROR', 'Ошибка запуска мониторинга')
        )

    await callback.answer(message, show_alert=True)

    await show_maintenance_panel(callback, db_user, db, None)


@admin_required
@error_handler
async def force_api_check(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('MAINTENANCE_API_CHECKING', 'Проверка API...'), show_alert=False)

    check_result = await maintenance_service.force_api_check()

    if check_result['success']:
        status_text = (
            texts.t('MAINTENANCE_API_AVAILABLE_LOWER', 'доступно')
            if check_result['api_available']
            else texts.t('MAINTENANCE_API_UNAVAILABLE_LOWER', 'недоступно')
        )
        message = texts.t('MAINTENANCE_API_CHECK_RESULT', 'API {status_text}\nВремя ответа: {response_time}с').format(
            status_text=status_text, response_time=check_result['response_time']
        )
    else:
        message = texts.t('MAINTENANCE_API_CHECK_ERROR', 'Ошибка проверки: {error}').format(
            error=check_result.get('error', texts.t('MAINTENANCE_UNKNOWN_ERROR', 'Неизвестная ошибка'))
        )

    await callback.message.answer(message)

    await show_maintenance_panel(callback, db_user, db, None)


@admin_required
@error_handler
async def check_panel_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('MAINTENANCE_PANEL_CHECKING', 'Проверка статуса панели...'), show_alert=False)

    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()

        status_data = await rw_service.check_panel_health()

        status_text = {
            'online': texts.t('MAINTENANCE_PANEL_HEALTH_ONLINE', '🟢 Панель работает нормально'),
            'offline': texts.t('MAINTENANCE_PANEL_HEALTH_OFFLINE', '🔴 Панель недоступна'),
            'degraded': texts.t('MAINTENANCE_PANEL_HEALTH_DEGRADED', '🟡 Панель работает со сбоями'),
        }.get(status_data['status'], texts.t('MAINTENANCE_PANEL_HEALTH_UNKNOWN', '❓ Статус неизвестен'))

        message_parts = [
            texts.t('MAINTENANCE_PANEL_HEALTH_HEADER', '🌐 <b>Статус панели Remnawave</b>\n'),
            f'{status_text}',
            texts.t('MAINTENANCE_PANEL_HEALTH_RESPONSE_TIME', '⚡ Время отклика: {response_time}с').format(
                response_time=status_data.get('response_time', 0)
            ),
            texts.t('MAINTENANCE_PANEL_HEALTH_USERS', '👥 Пользователей онлайн: {users_online}').format(
                users_online=status_data.get('users_online', 0)
            ),
            texts.t('MAINTENANCE_PANEL_HEALTH_NODES', '🖥️ Нод онлайн: {nodes_online}/{total_nodes}').format(
                nodes_online=status_data.get('nodes_online', 0), total_nodes=status_data.get('total_nodes', 0)
            ),
        ]

        attempts_used = status_data.get('attempts_used')
        if attempts_used:
            message_parts.append(
                texts.t('MAINTENANCE_PANEL_HEALTH_ATTEMPTS', '🔁 Попыток проверки: {attempts_used}').format(
                    attempts_used=attempts_used
                )
            )

        if status_data.get('api_error'):
            message_parts.append(
                texts.t('MAINTENANCE_ERROR_LINE', '❌ Ошибка: {error}').format(error=status_data['api_error'][:100])
            )

        message = '\n'.join(message_parts)

        await callback.message.answer(message, parse_mode='HTML')

    except Exception as e:
        await callback.message.answer(
            texts.t('MAINTENANCE_PANEL_HEALTH_CHECK_ERROR', '❌ Ошибка проверки статуса: {error}').format(
                error=str(e)
            )
        )


@admin_required
@error_handler
async def send_manual_notification(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    await state.set_state(MaintenanceStates.waiting_for_notification_message)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('MAINTENANCE_NOTIFY_STATUS_ONLINE', '🟢 Онлайн'),
                    callback_data='manual_notify_online',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('MAINTENANCE_NOTIFY_STATUS_OFFLINE', '🔴 Офлайн'),
                    callback_data='manual_notify_offline',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('MAINTENANCE_NOTIFY_STATUS_DEGRADED', '🟡 Проблемы'),
                    callback_data='manual_notify_degraded',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('MAINTENANCE_NOTIFY_STATUS_MAINTENANCE', '🔧 Обслуживание'),
                    callback_data='manual_notify_maintenance',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='maintenance_panel'
                )
            ],
        ]
    )

    await callback.message.edit_text(
        texts.t(
            'MAINTENANCE_NOTIFY_PROMPT',
            '📢 <b>Ручная отправка уведомления</b>\n\nВыберите статус для уведомления:',
        ),
        reply_markup=keyboard,
    )


@admin_required
@error_handler
async def handle_manual_notification(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    status_map = {
        'manual_notify_online': 'online',
        'manual_notify_offline': 'offline',
        'manual_notify_degraded': 'degraded',
        'manual_notify_maintenance': 'maintenance',
    }

    status = status_map.get(callback.data)
    if not status:
        await callback.answer(texts.t('MAINTENANCE_NOTIFY_UNKNOWN_STATUS', 'Неизвестный статус'))
        return

    await state.update_data(notification_status=status)

    status_names = {
        'online': texts.t('MAINTENANCE_NOTIFY_STATUS_ONLINE', '🟢 Онлайн'),
        'offline': texts.t('MAINTENANCE_NOTIFY_STATUS_OFFLINE', '🔴 Офлайн'),
        'degraded': texts.t('MAINTENANCE_NOTIFY_STATUS_DEGRADED', '🟡 Проблемы'),
        'maintenance': texts.t('MAINTENANCE_NOTIFY_STATUS_MAINTENANCE', '🔧 Обслуживание'),
    }

    await callback.message.edit_text(
        texts.t(
            'MAINTENANCE_NOTIFY_MESSAGE_PROMPT',
            '📢 <b>Отправка уведомления: {status}</b>\n\n'
            'Введите сообщение для уведомления или отправьте /skip для отправки без дополнительного текста:',
        ).format(status=status_names[status]),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='maintenance_panel'
                )
            ]]
        ),
    )


@admin_required
@error_handler
async def process_notification_message(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    current_state = await state.get_state()

    if current_state != MaintenanceStates.waiting_for_notification_message:
        return

    data = await state.get_data()
    status = data.get('notification_status')

    if not status:
        await message.answer(texts.t('MAINTENANCE_NOTIFY_NO_STATUS', 'Ошибка: статус не выбран'))
        await state.clear()
        return

    notification_message = ''
    if message.text and message.text != '/skip':
        notification_message = message.text[:300]

    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()

        success = await rw_service.send_manual_status_notification(message.bot, status, notification_message)

        if success:
            await message.answer(texts.t('MAINTENANCE_NOTIFY_SENT', '✅ Уведомление отправлено'))
        else:
            await message.answer(texts.t('MAINTENANCE_NOTIFY_SEND_ERROR', '❌ Ошибка отправки уведомления'))

    except Exception as e:
        logger.error('Ошибка отправки ручного уведомления', error=e)
        await message.answer(texts.t('MAINTENANCE_ERROR_LINE', '❌ Ошибка: {error}').format(error=str(e)))

    await state.clear()

    await message.answer(
        texts.t('MAINTENANCE_RETURN_PROMPT_SHORT', 'Вернуться к панели техработ:'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[
                types.InlineKeyboardButton(
                    text=texts.t('MAINTENANCE_PANEL_BUTTON', '🔧 Панель техработ'), callback_data='maintenance_panel'
                )
            ]]
        ),
    )


@admin_required
@error_handler
async def back_to_admin_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(texts.ADMIN_PANEL, reply_markup=get_admin_main_keyboard(db_user.language))
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_maintenance_panel, F.data == 'maintenance_panel')

    dp.callback_query.register(toggle_maintenance_mode, F.data == 'maintenance_toggle')

    dp.callback_query.register(toggle_monitoring, F.data == 'maintenance_monitoring')

    dp.callback_query.register(force_api_check, F.data == 'maintenance_check_api')

    dp.callback_query.register(check_panel_status, F.data == 'maintenance_check_panel')

    dp.callback_query.register(send_manual_notification, F.data == 'maintenance_manual_notify')

    dp.callback_query.register(handle_manual_notification, F.data.startswith('manual_notify_'))

    dp.callback_query.register(back_to_admin_panel, F.data == 'admin_panel')

    dp.message.register(process_maintenance_reason, MaintenanceStates.waiting_for_reason)

    dp.message.register(process_notification_message, MaintenanceStates.waiting_for_notification_message)
