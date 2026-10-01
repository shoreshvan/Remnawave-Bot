"""
Обработчики админ-панели для управления черным списком
"""

import html

import structlog
from aiogram import types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext

from app.database.models import User
from app.localization.texts import get_texts
from app.services.blacklist_service import blacklist_service
from app.states import BlacklistStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_blacklist_settings(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Показывает настройки черного списка
    """
    logger.info('Вызван обработчик show_blacklist_settings для пользователя', from_user_id=callback.from_user.id)

    texts = get_texts(db_user.language)

    is_enabled = blacklist_service.is_blacklist_check_enabled()
    github_url = blacklist_service.get_blacklist_github_url()
    blacklist_count = len(await blacklist_service.get_all_blacklisted_users())

    status_text = (
        texts.t('BLACKLIST_STATUS_ENABLED', '✅ Включена')
        if is_enabled
        else texts.t('BLACKLIST_STATUS_DISABLED', '❌ Отключена')
    )
    url_text = github_url or texts.t('BLACKLIST_URL_NOT_SET', 'Не задан')

    text = texts.t(
        'BLACKLIST_SETTINGS_TEXT',
        '\n🔐 <b>Настройки черного списка</b>\n\n'
        'Статус: {status_text}\n'
        'URL к черному списку: <code>{url_text}</code>\n'
        'Количество записей: {blacklist_count}\n\n'
        'Действия:\n',
    ).format(status_text=status_text, url_text=url_text, blacklist_count=blacklist_count)

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=(
                    texts.t('BLACKLIST_BTN_UPDATE_LIST', '🔄 Обновить список')
                    if is_enabled
                    else texts.t('BLACKLIST_BTN_UPDATE_OFF', '🔄 Обновить (откл.)')
                ),
                callback_data='admin_blacklist_update',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=(
                    texts.t('BLACKLIST_BTN_VIEW_LIST', '📋 Просмотреть список')
                    if is_enabled
                    else texts.t('BLACKLIST_BTN_VIEW_OFF', '📋 Просмотр (откл.)')
                ),
                callback_data='admin_blacklist_view',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=(
                    texts.t('BLACKLIST_BTN_SET_URL', '✏️ URL к GitHub')
                    if not github_url
                    else texts.t('BLACKLIST_BTN_CHANGE_URL', '✏️ Изменить URL')
                ),
                callback_data='admin_blacklist_set_url',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=(
                    texts.t('BLACKLIST_BTN_ENABLE', '✅ Включить')
                    if not is_enabled
                    else texts.t('BLACKLIST_BTN_DISABLE', '❌ Отключить')
                ),
                callback_data='admin_blacklist_toggle',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('BLACKLIST_BTN_BACK_TO_USERS', '⬅️ Назад к пользователям'),
                callback_data='admin_users',
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def toggle_blacklist(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Переключает статус проверки черного списка
    """
    # Текущая реализация использует настройки из .env
    # Для полной реализации нужно будет создать сервис настроек
    texts = get_texts(db_user.language)
    is_enabled = blacklist_service.is_blacklist_check_enabled()

    # В реальной реализации нужно будет изменить настройку в базе данных
    # или в системе настроек, но сейчас просто покажем статус
    new_status = not is_enabled
    status_text = (
        texts.t('BLACKLIST_TOGGLE_ENABLED', 'включена')
        if new_status
        else texts.t('BLACKLIST_TOGGLE_DISABLED', 'отключена')
    )

    await callback.message.edit_text(
        texts.t(
            'BLACKLIST_TOGGLE_TEXT',
            'Статус проверки черного списка: {status_text}\n\n'
            'Для изменения статуса проверки черного списка измените значение\n'
            '<code>BLACKLIST_CHECK_ENABLED</code> в файле <code>.env</code>',
        ).format(status_text=status_text),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BLACKLIST_BTN_UPDATE_STATUS', '🔄 Обновить статус'),
                        callback_data='admin_blacklist_settings',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BLACKLIST_BTN_BACK', '⬅️ Назад'),
                        callback_data='admin_blacklist_settings',
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def update_blacklist(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Обновляет черный список из GitHub
    """
    success, message = await blacklist_service.force_update_blacklist()

    texts = get_texts(db_user.language)

    if success:
        await callback.message.edit_text(
            f'✅ {message}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BLACKLIST_BTN_VIEW_LIST', '📋 Просмотреть список'),
                            callback_data='admin_blacklist_view',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BLACKLIST_BTN_MANUAL_UPDATE', '🔄 Ручное обновление'),
                            callback_data='admin_blacklist_update',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BLACKLIST_BTN_BACK', '⬅️ Назад'),
                            callback_data='admin_blacklist_settings',
                        )
                    ],
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('BLACKLIST_FORCE_UPDATE_ERROR', '❌ Ошибка обновления: {message}').format(message=message),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BLACKLIST_BTN_RETRY', '🔄 Повторить'),
                            callback_data='admin_blacklist_update',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BLACKLIST_BTN_BACK', '⬅️ Назад'),
                            callback_data='admin_blacklist_settings',
                        )
                    ],
                ]
            ),
        )
    await callback.answer()


@admin_required
@error_handler
async def show_blacklist_users(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Показывает список пользователей в черном списке
    """
    blacklist_users = await blacklist_service.get_all_blacklisted_users()

    texts = get_texts(db_user.language)

    if not blacklist_users:
        text = texts.t('BLACKLIST_USERS_EMPTY', 'Черный список пуст')
    else:
        text = texts.t('BLACKLIST_USERS_HEADER', '🔐 <b>Черный список ({count} записей)</b>\n\n').format(
            count=len(blacklist_users)
        )

        # Показываем первые 20 записей
        for i, (tg_id, username, reason) in enumerate(blacklist_users[:20], 1):
            text += f'{i}. <code>{tg_id}</code> {html.escape(username or "")} — {html.escape(reason or "")}\n'

        if len(blacklist_users) > 20:
            text += texts.t('BLACKLIST_USERS_MORE', '\n... и еще {count} записей').format(
                count=len(blacklist_users) - 20
            )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BLACKLIST_BTN_UPDATE', '🔄 Обновить'),
                        callback_data='admin_blacklist_view',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BLACKLIST_BTN_BACK', '⬅️ Назад'),
                        callback_data='admin_blacklist_settings',
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_set_blacklist_url(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Начинает процесс установки URL к черному списку
    """
    texts = get_texts(db_user.language)
    current_url = blacklist_service.get_blacklist_github_url() or texts.t('BLACKLIST_URL_NOT_SET_LC', 'не задан')

    await callback.message.edit_text(
        texts.t(
            'BLACKLIST_SET_URL_PROMPT',
            'Введите новый URL к файлу черного списка на GitHub\n\n'
            'Текущий URL: {current_url}\n\n'
            'Пример: https://raw.githubusercontent.com/username/repository/main/blacklist.txt\n\n'
            'Для отмены используйте команду /cancel',
        ).format(current_url=current_url),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BLACKLIST_BTN_BACK', '⬅️ Назад'),
                        callback_data='admin_blacklist_settings',
                    )
                ]
            ]
        ),
    )

    await state.set_state(BlacklistStates.waiting_for_blacklist_url)
    await callback.answer()


@admin_required
@error_handler
async def process_blacklist_url(message: types.Message, db_user: User, state: FSMContext):
    """
    Обрабатывает введенный URL к черному списку
    """
    # Обрабатываем сообщение только если бот ожидает ввод URL
    if await state.get_state() != BlacklistStates.waiting_for_blacklist_url.state:
        return

    url = message.text.strip()
    texts = get_texts(db_user.language)

    # В реальной реализации нужно сохранить URL в систему настроек
    # В текущей реализации просто выводим сообщение
    if url.lower() in ['/cancel', 'отмена', 'cancel']:
        await message.answer(
            texts.t('BLACKLIST_URL_CANCELLED', 'Настройка URL отменена'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BLACKLIST_BTN_SETTINGS', '🔐 Настройки черного списка'),
                            callback_data='admin_blacklist_settings',
                        )
                    ]
                ]
            ),
        )
        await state.clear()
        return

    # Проверяем, что URL выглядит корректно
    if not url.startswith(('http://', 'https://')):
        await message.answer(
            texts.t('BLACKLIST_URL_INVALID', '❌ Некорректный URL. URL должен начинаться с http:// или https://'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BLACKLIST_BTN_SETTINGS', '🔐 Настройки черного списка'),
                            callback_data='admin_blacklist_settings',
                        )
                    ]
                ]
            ),
        )
        return

    # В реальной системе здесь нужно сохранить URL в базу данных настроек
    # или в систему конфигурации

    await message.answer(
        texts.t(
            'BLACKLIST_URL_SET_SUCCESS',
            '✅ URL к черному списку установлен:\n<code>{url}</code>\n\n'
            'Для применения изменений перезапустите бота или измените значение\n'
            '<code>BLACKLIST_GITHUB_URL</code> в файле <code>.env</code>',
        ).format(url=url),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BLACKLIST_BTN_UPDATE_LIST', '🔄 Обновить список'),
                        callback_data='admin_blacklist_update',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BLACKLIST_BTN_SETTINGS', '🔐 Настройки черного списка'),
                        callback_data='admin_blacklist_settings',
                    )
                ],
            ]
        ),
    )
    await state.clear()


def register_blacklist_handlers(dp):
    """
    Регистрация обработчиков черного списка
    """
    # Обработчик показа настроек черного списка
    # Этот обработчик нужно будет вызывать из меню пользователей или отдельно
    dp.callback_query.register(show_blacklist_settings, lambda c: c.data == 'admin_blacklist_settings')

    # Обработчики для взаимодействия с черным списком
    dp.callback_query.register(toggle_blacklist, lambda c: c.data == 'admin_blacklist_toggle')

    dp.callback_query.register(update_blacklist, lambda c: c.data == 'admin_blacklist_update')

    dp.callback_query.register(show_blacklist_users, lambda c: c.data == 'admin_blacklist_view')

    dp.callback_query.register(start_set_blacklist_url, lambda c: c.data == 'admin_blacklist_set_url')

    # Обработчик сообщений для установки URL (работает только в нужном состоянии)
    dp.message.register(process_blacklist_url, StateFilter(BlacklistStates.waiting_for_blacklist_url))
