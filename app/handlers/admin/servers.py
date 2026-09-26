import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_groups_with_counts
from app.database.crud.server_squad import (
    delete_server_squad,
    get_all_server_squads,
    get_available_server_squads,
    get_server_connected_users,
    get_server_squad_by_id,
    get_server_statistics,
    sync_with_remnawave,
    update_server_squad,
    update_server_squad_promo_groups,
)
from app.database.models import User
from app.localization.texts import get_texts
from app.services.remnawave_service import RemnaWaveService
from app.states import AdminStates
from app.utils.cache import cache
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def _build_server_edit_view(server):
    status_emoji = '✅ Доступен' if server.is_available else '❌ Недоступен'
    price_text = f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else 'Бесплатно'
    promo_groups_text = (
        ', '.join(sorted(pg.name for pg in server.allowed_promo_groups))
        if server.allowed_promo_groups
        else 'Не выбраны'
    )

    trial_status = '✅ Да' if server.is_trial_eligible else '⚪️ Нет'

    text = f"""
🌐 <b>Редактирование сервера</b>

<b>Информация:</b>
• ID: {server.id}
• UUID: <code>{server.squad_uuid}</code>
• Название: {html.escape(server.display_name)}
• Оригинальное: {html.escape(server.original_name) if server.original_name else 'Не указано'}
• Статус: {status_emoji}

<b>Настройки:</b>
• Цена: {price_text}
• Код страны: {server.country_code or 'Не указан'}
• Лимит пользователей: {server.max_users or 'Без лимита'}
• Текущих пользователей: {server.current_users}
• Промогруппы: {promo_groups_text}
• Выдача триала: {trial_status}

<b>Описание:</b>
{server.description or 'Не указано'}

Выберите что изменить:
"""

    keyboard = [
        [
            types.InlineKeyboardButton(text='✏️ Название', callback_data=f'admin_server_edit_name_{server.id}'),
            types.InlineKeyboardButton(text='💰 Цена', callback_data=f'admin_server_edit_price_{server.id}'),
        ],
        [
            types.InlineKeyboardButton(text='🌍 Страна', callback_data=f'admin_server_edit_country_{server.id}'),
            types.InlineKeyboardButton(text='👥 Лимит', callback_data=f'admin_server_edit_limit_{server.id}'),
        ],
        [
            types.InlineKeyboardButton(text='👥 Юзеры', callback_data=f'admin_server_users_{server.id}'),
        ],
        [
            types.InlineKeyboardButton(
                text='🎁 Выдавать в триал' if not server.is_trial_eligible else '🚫 Не выдавать в триал',
                callback_data=f'admin_server_trial_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(text='🎯 Промогруппы', callback_data=f'admin_server_edit_promo_{server.id}'),
            types.InlineKeyboardButton(text='📝 Описание', callback_data=f'admin_server_edit_desc_{server.id}'),
        ],
        [
            types.InlineKeyboardButton(
                text='❌ Отключить' if server.is_available else '✅ Включить',
                callback_data=f'admin_server_toggle_{server.id}',
            )
        ],
        [
            types.InlineKeyboardButton(text='🗑️ Удалить', callback_data=f'admin_server_delete_{server.id}'),
            types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_servers_list'),
        ],
    ]

    return text, types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _build_server_promo_groups_keyboard(server_id: int, promo_groups, selected_ids):
    keyboard = []
    for group in promo_groups:
        emoji = '✅' if group['id'] in selected_ids else '⚪'
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'{emoji} {group["name"]}',
                    callback_data=f'admin_server_promo_toggle_{server_id}_{group["id"]}',
                )
            ]
        )

    keyboard.append(
        [types.InlineKeyboardButton(text='💾 Сохранить', callback_data=f'admin_server_promo_save_{server_id}')]
    )
    keyboard.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data=f'admin_server_edit_{server_id}')])

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


@admin_required
@error_handler
async def show_servers_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_server_statistics(db)

    text = texts.t(
        'ADMIN_SERVERS_MENU_TEXT',
        """
🌐 <b>Управление серверами</b>

📊 <b>Статистика:</b>
• Всего серверов: {total_servers}
• Доступные: {available_servers}
• Недоступные: {unavailable_servers}
• С подключениями: {servers_with_connections}

💰 <b>Выручка от серверов:</b>
• Общая: {total_revenue} ₽

Выберите действие:
""",
    ).format(
        total_servers=stats['total_servers'],
        available_servers=stats['available_servers'],
        unavailable_servers=stats['unavailable_servers'],
        servers_with_connections=stats['servers_with_connections'],
        total_revenue=int(stats['total_revenue_rubles']),
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_BTN_LIST', '📋 Список серверов'),
                callback_data='admin_servers_list',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_BTN_SYNC', '🔄 Синхронизация'),
                callback_data='admin_servers_sync',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_BTN_SYNC_COUNTS', '📊 Синхронизировать счетчики'),
                callback_data='admin_servers_sync_counts',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_BTN_DETAILED_STATS', '📈 Подробная статистика'),
                callback_data='admin_servers_stats',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_BTN_BACK', '⬅️ Назад'), callback_data='admin_panel'
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_servers_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    texts = get_texts(db_user.language)
    servers, total_count = await get_all_server_squads(db, page=page, limit=10)
    total_pages = (total_count + 9) // 10

    if not servers:
        text = texts.t('ADMIN_SERVERS_LIST_EMPTY', '🌐 <b>Список серверов</b>\n\n❌ Серверы не найдены.')
    else:
        text = texts.t('ADMIN_SERVERS_LIST_HEADER', '🌐 <b>Список серверов</b>\n\n')
        text += texts.t('ADMIN_SERVERS_LIST_COUNT', '📊 Всего: {total} | Страница: {page}/{total_pages}\n\n').format(
            total=total_count, page=page, total_pages=total_pages
        )

        for i, server in enumerate(servers, 1 + (page - 1) * 10):
            status_emoji = '✅' if server.is_available else '❌'
            price_text = (
                f'{int(server.price_rubles)} ₽'
                if server.price_kopeks > 0
                else texts.t('ADMIN_SERVERS_FREE', 'Бесплатно')
            )

            text += f'{i}. {status_emoji} {html.escape(server.display_name)}\n'
            text += f'   💰 Цена: {price_text}'

            if server.max_users:
                text += f' | 👥 {server.current_users}/{server.max_users}'

            text += f'\n   UUID: <code>{server.squad_uuid}</code>\n\n'

    keyboard = []

    for i, server in enumerate(servers):
        row_num = i // 2
        if len(keyboard) <= row_num:
            keyboard.append([])

        status_emoji = '✅' if server.is_available else '❌'
        keyboard[row_num].append(
            types.InlineKeyboardButton(
                text=f'{status_emoji} {server.display_name[:15]}...', callback_data=f'admin_server_edit_{server.id}'
            )
        )

    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text='⬅️', callback_data=f'admin_servers_list_page_{page - 1}'))

        nav_row.append(types.InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='current_page'))

        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text='➡️', callback_data=f'admin_servers_list_page_{page + 1}'))

        keyboard.append(nav_row)

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SERVERS_BTN_BACK', '⬅️ Назад'),
                    callback_data='admin_servers',
                )
            ]
        ]
    )

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def sync_servers_with_remnawave(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_SERVERS_SYNC_PROGRESS',
            '🔄 Синхронизация с Remnawave...\n\nПодождите, это может занять время.',
        ),
        reply_markup=None,
    )

    try:
        remnawave_service = RemnaWaveService()
        squads = await remnawave_service.get_all_squads()

        if not squads:
            await callback.message.edit_text(
                texts.t(
                    'ADMIN_SERVERS_SYNC_NO_DATA',
                    '❌ Не удалось получить данные о сквадах из Remnawave.\n\nПроверьте настройки API.',
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_SERVERS_BTN_BACK', '⬅️ Назад'),
                                callback_data='admin_servers',
                            )
                        ]
                    ]
                ),
            )
            return

        created, updated, removed = await sync_with_remnawave(db, squads)

        await cache.delete_pattern('available_countries*')

        text = texts.t(
            'ADMIN_SERVERS_SYNC_DONE',
            """
✅ <b>Синхронизация завершена</b>

📊 <b>Результаты:</b>
• Создано новых серверов: {created}
• Обновлено существующих: {updated}
• Удалено отсутствующих: {removed}
• Всего обработано: {total}

ℹ️ Новые серверы созданы как недоступные.
Настройте их в списке серверов.
""",
        ).format(created=created, updated=updated, removed=removed, total=len(squads))

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SERVERS_BTN_LIST', '📋 Список серверов'),
                    callback_data='admin_servers_list',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SERVERS_BTN_RETRY', '🔄 Повторить'),
                    callback_data='admin_servers_sync',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SERVERS_BTN_BACK', '⬅️ Назад'),
                    callback_data='admin_servers',
                )
            ],
        ]

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error('Ошибка синхронизации серверов', error=e)
        await callback.message.edit_text(
            texts.t('ADMIN_SERVERS_SYNC_ERROR', '❌ Ошибка синхронизации: {error}').format(error=e),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SERVERS_BTN_BACK', '⬅️ Назад'),
                            callback_data='admin_servers',
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def show_server_edit_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND', '❌ Сервер не найден!'), show_alert=True)
        return

    text, keyboard = _build_server_edit_view(server)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_server_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    payload = callback.data.split('admin_server_users_', 1)[-1]
    payload_parts = payload.split('_')

    server_id = int(payload_parts[0])
    page = int(payload_parts[1]) if len(payload_parts) > 1 else 1
    page = max(page, 1)
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND', '❌ Сервер не найден!'), show_alert=True)
        return

    users = await get_server_connected_users(db, server_id)
    total_users = len(users)

    page_size = 10
    total_pages = max((total_users + page_size - 1) // page_size, 1)

    page = min(page, total_pages)

    start_index = (page - 1) * page_size
    end_index = start_index + page_size
    page_users = users[start_index:end_index]

    safe_name = html.escape(server.display_name or '—')
    safe_uuid = html.escape(server.squad_uuid or '—')

    header = [
        texts.t('ADMIN_SERVER_USERS_TITLE', '🌐 <b>Пользователи сервера</b>'),
        '',
        texts.t('ADMIN_SERVER_USERS_ROW_SERVER', '• Сервер: {name}').format(name=safe_name),
        texts.t('ADMIN_SERVER_USERS_ROW_UUID', '• UUID: <code>{uuid}</code>').format(uuid=safe_uuid),
        texts.t('ADMIN_SERVER_USERS_ROW_CONNECTIONS', '• Подключений: {count}').format(count=total_users),
    ]

    if total_pages > 1:
        header.append(
            texts.t('ADMIN_SERVER_USERS_ROW_PAGE', '• Страница: {page}/{total_pages}').format(
                page=page, total_pages=total_pages
            )
        )

    header.append('')

    text = '\n'.join(header)

    def _get_status_icon(status_text: str) -> str:
        if not status_text:
            return ''

        parts = status_text.split(' ', 1)
        return parts[0] if parts else status_text

    if users:
        lines = []
        for index, user in enumerate(page_users, start=start_index + 1):
            safe_user_name = html.escape(user.full_name)
            if user.telegram_id:
                user_link = f'<a href="tg://user?id={user.telegram_id}">{safe_user_name}</a>'
            else:
                user_link = f'<b>{safe_user_name}</b>'
            lines.append(f'{index}. {user_link}')

        text += '\n' + '\n'.join(lines)
    else:
        text += texts.t('ADMIN_SERVER_USERS_EMPTY', 'Пользователи не найдены.')

    keyboard: list[list[types.InlineKeyboardButton]] = []

    for user in page_users:
        display_name = user.full_name
        if len(display_name) > 30:
            display_name = display_name[:27] + '...'

        if settings.is_multi_tariff_enabled() and hasattr(user, 'subscriptions') and user.subscriptions:
            status_parts = []
            for sub in user.subscriptions:
                emoji = '🟢' if sub.is_active else '🔴'
                name = sub.tariff.name if sub.tariff else f'#{sub.id}'
                status_parts.append(f'{emoji}{name}')
            subscription_status = ', '.join(status_parts)
        elif user.subscription:
            subscription_status = user.subscription.status_display
        else:
            subscription_status = texts.t('ADMIN_SERVER_USERS_NO_SUB', '❌ Нет подписки')
        status_icon = _get_status_icon(subscription_status)

        if status_icon:
            button_text = f'{status_icon} {display_name}'
        else:
            button_text = display_name

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=f'admin_user_manage_{user.id}',
                )
            ]
        )

    if total_pages > 1:
        navigation_buttons: list[types.InlineKeyboardButton] = []

        if page > 1:
            navigation_buttons.append(
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SERVERS_BTN_PREV', '⬅️ Предыдущая'),
                    callback_data=f'admin_server_users_{server_id}_{page - 1}',
                )
            )

        navigation_buttons.append(
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_BTN_PAGE_INFO', 'Стр. {page}/{total_pages}').format(
                    page=page, total_pages=total_pages
                ),
                callback_data=f'admin_server_users_{server_id}_{page}',
            )
        )

        if page < total_pages:
            navigation_buttons.append(
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SERVERS_BTN_NEXT', 'Следующая ➡️'),
                    callback_data=f'admin_server_users_{server_id}_{page + 1}',
                )
            )

        keyboard.append(navigation_buttons)

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_BTN_BACK_TO_SERVER', '⬅️ К серверу'),
                callback_data=f'admin_server_edit_{server_id}',
            )
        ]
    )

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_BTN_BACK_TO_LIST', '⬅️ К списку'),
                callback_data='admin_servers_list',
            )
        ]
    )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode='HTML',
    )

    await callback.answer()


@admin_required
@error_handler
async def toggle_server_availability(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND', '❌ Сервер не найден!'), show_alert=True)
        return

    new_status = not server.is_available
    await update_server_squad(db, server_id, is_available=new_status)

    await cache.delete_pattern('available_countries*')

    status_text = (
        texts.t('ADMIN_SERVERS_STATUS_ENABLED', 'включен')
        if new_status
        else texts.t('ADMIN_SERVERS_STATUS_DISABLED', 'отключен')
    )
    await callback.answer(texts.t('ADMIN_SERVERS_TOGGLED', '✅ Сервер {status}!').format(status=status_text))

    server = await get_server_squad_by_id(db, server_id)

    text, keyboard = _build_server_edit_view(server)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def toggle_server_trial_assignment(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND', '❌ Сервер не найден!'), show_alert=True)
        return

    new_status = not server.is_trial_eligible
    await update_server_squad(db, server_id, is_trial_eligible=new_status)

    status_text = (
        texts.t('ADMIN_SERVERS_TRIAL_WILL', 'будет выдаваться')
        if new_status
        else texts.t('ADMIN_SERVERS_TRIAL_WONT', 'перестанет выдаваться')
    )
    await callback.answer(
        texts.t('ADMIN_SERVERS_TRIAL_TOGGLED', '✅ Сквад {status} в триал').format(status=status_text)
    )

    server = await get_server_squad_by_id(db, server_id)

    text, keyboard = _build_server_edit_view(server)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def start_server_edit_price(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND', '❌ Сервер не найден!'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_price)

    current_price = (
        f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else texts.t('ADMIN_SERVERS_FREE', 'Бесплатно')
    )

    await callback.message.edit_text(
        texts.t(
            'ADMIN_SERVERS_EDIT_PRICE_PROMPT',
            '💰 <b>Редактирование цены</b>\n\n'
            'Текущая цена: <b>{price}</b>\n\n'
            'Отправьте новую цену в рублях (например: 15.50) или 0 для бесплатного доступа:',
        ).format(price=current_price),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_SERVERS_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_server_edit_{server_id}',
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_price_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    server_id = data.get('server_id')

    try:
        price_rubles = float(message.text.replace(',', '.'))

        if price_rubles < 0:
            await message.answer(texts.t('ADMIN_SERVERS_PRICE_NEGATIVE', '❌ Цена не может быть отрицательной'))
            return

        if price_rubles > 10000:
            await message.answer(
                texts.t('ADMIN_SERVERS_PRICE_TOO_HIGH', '❌ Слишком высокая цена (максимум 10,000 ₽)')
            )
            return

        price_kopeks = int(price_rubles * 100)

        server = await update_server_squad(db, server_id, price_kopeks=price_kopeks)

        if server:
            await state.clear()

            await cache.delete_pattern('available_countries*')

            price_text = (
                f'{int(price_rubles)} ₽' if price_kopeks > 0 else texts.t('ADMIN_SERVERS_FREE', 'Бесплатно')
            )
            await message.answer(
                texts.t('ADMIN_SERVERS_PRICE_UPDATED', '✅ Цена сервера изменена на: <b>{price}</b>').format(
                    price=price_text
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_SERVERS_BTN_TO_SERVER', '🔙 К серверу'),
                                callback_data=f'admin_server_edit_{server_id}',
                            )
                        ]
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            await message.answer(texts.t('ADMIN_SERVERS_UPDATE_ERROR', '❌ Ошибка при обновлении сервера'))

    except ValueError:
        await message.answer(
            texts.t('ADMIN_SERVERS_PRICE_INVALID', '❌ Неверный формат цены. Используйте числа (например: 15.50)')
        )


@admin_required
@error_handler
async def start_server_edit_name(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND', '❌ Сервер не найден!'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_name)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_SERVERS_EDIT_NAME_PROMPT',
            '✏️ <b>Редактирование названия</b>\n\n'
            'Текущее название: <b>{name}</b>\n\n'
            'Отправьте новое название для сервера:',
        ).format(name=html.escape(server.display_name)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_SERVERS_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_server_edit_{server_id}',
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_name_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    server_id = data.get('server_id')

    new_name = message.text.strip()

    if len(new_name) > 255:
        await message.answer(
            texts.t('ADMIN_SERVERS_NAME_TOO_LONG', '❌ Название слишком длинное (максимум 255 символов)')
        )
        return

    if len(new_name) < 3:
        await message.answer(
            texts.t('ADMIN_SERVERS_NAME_TOO_SHORT', '❌ Название слишком короткое (минимум 3 символа)')
        )
        return

    server = await update_server_squad(db, server_id, display_name=new_name)

    if server:
        await state.clear()

        await cache.delete_pattern('available_countries*')

        await message.answer(
            texts.t('ADMIN_SERVERS_NAME_UPDATED', '✅ Название сервера изменено на: <b>{name}</b>').format(
                name=new_name
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SERVERS_BTN_TO_SERVER', '🔙 К серверу'),
                            callback_data=f'admin_server_edit_{server_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer(texts.t('ADMIN_SERVERS_UPDATE_ERROR', '❌ Ошибка при обновлении сервера'))


@admin_required
@error_handler
async def delete_server_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND', '❌ Сервер не найден!'), show_alert=True)
        return

    text = texts.t(
        'ADMIN_SERVERS_DELETE_CONFIRM_TEXT',
        """
🗑️ <b>Удаление сервера</b>

Вы действительно хотите удалить сервер:
<b>{name}</b>

⚠️ <b>Внимание!</b>
Сервер можно удалить только если к нему нет активных подключений.

Это действие нельзя отменить!
""",
    ).format(name=html.escape(server.display_name))

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_BTN_DELETE_YES', '🗑️ Да, удалить'),
                callback_data=f'admin_server_delete_confirm_{server_id}',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_BTN_CANCEL', '❌ Отмена'),
                callback_data=f'admin_server_edit_{server_id}',
            ),
        ]
    ]

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_server_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND', '❌ Сервер не найден!'), show_alert=True)
        return

    success = await delete_server_squad(db, server_id)

    if success:
        await cache.delete_pattern('available_countries*')

        await callback.message.edit_text(
            texts.t('ADMIN_SERVERS_DELETE_SUCCESS', '✅ Сервер <b>{name}</b> успешно удален!').format(
                name=html.escape(server.display_name)
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SERVERS_BTN_TO_LIST', '📋 К списку серверов'),
                            callback_data='admin_servers_list',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_SERVERS_DELETE_FAILED',
                '❌ Не удалось удалить сервер <b>{name}</b>\n\nВозможно, к нему есть активные подключения.',
            ).format(name=html.escape(server.display_name)),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SERVERS_BTN_TO_SERVER', '🔙 К серверу'),
                            callback_data=f'admin_server_edit_{server_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )

    await callback.answer()


@admin_required
@error_handler
async def show_server_detailed_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_server_statistics(db)
    available_servers = await get_available_server_squads(db)

    text = texts.t(
        'ADMIN_SERVERS_STATS_TEXT',
        """
📊 <b>Подробная статистика серверов</b>

<b>🌐 Общая информация:</b>
• Всего серверов: {total_servers}
• Доступные: {available_servers}
• Недоступные: {unavailable_servers}
• С активными подключениями: {servers_with_connections}

<b>💰 Финансовая статистика:</b>
• Общая выручка: {total_revenue} ₽
• Средняя цена за сервер: {avg_price} ₽

<b>🔥 Топ серверов по цене:</b>
""",
    ).format(
        total_servers=stats['total_servers'],
        available_servers=stats['available_servers'],
        unavailable_servers=stats['unavailable_servers'],
        servers_with_connections=stats['servers_with_connections'],
        total_revenue=int(stats['total_revenue_rubles']),
        avg_price=int(stats['total_revenue_rubles'] / max(stats['servers_with_connections'], 1)),
    )

    sorted_servers = sorted(available_servers, key=lambda x: x.price_kopeks, reverse=True)

    for i, server in enumerate(sorted_servers[:5], 1):
        price_text = (
            f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else texts.t('ADMIN_SERVERS_FREE', 'Бесплатно')
        )
        text += f'{i}. {html.escape(server.display_name)} - {price_text}\n'

    if not sorted_servers:
        text += texts.t('ADMIN_SERVERS_STATS_NO_SERVERS', 'Нет доступных серверов\n')

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_BTN_REFRESH', '🔄 Обновить'), callback_data='admin_servers_stats'
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_BTN_LIST_SHORT', '📋 Список'), callback_data='admin_servers_list'
            ),
        ],
        [types.InlineKeyboardButton(text=texts.t('ADMIN_SERVERS_BTN_BACK', '⬅️ Назад'), callback_data='admin_servers')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def start_server_edit_country(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND', '❌ Сервер не найден!'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_country)

    current_country = server.country_code or texts.t('ADMIN_SERVERS_COUNTRY_NOT_SET', 'Не указан')

    await callback.message.edit_text(
        texts.t(
            'ADMIN_SERVERS_EDIT_COUNTRY_PROMPT',
            '🌍 <b>Редактирование кода страны</b>\n\n'
            'Текущий код страны: <b>{country}</b>\n\n'
            "Отправьте новый код страны (например: RU, US, DE) или '-' для удаления:",
        ).format(country=current_country),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_SERVERS_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_server_edit_{server_id}',
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_country_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    server_id = data.get('server_id')

    new_country = message.text.strip().upper()

    if new_country == '-':
        new_country = None
    elif len(new_country) > 5:
        await message.answer(
            texts.t('ADMIN_SERVERS_COUNTRY_TOO_LONG', '❌ Код страны слишком длинный (максимум 5 символов)')
        )
        return

    server = await update_server_squad(db, server_id, country_code=new_country)

    if server:
        await state.clear()

        await cache.delete_pattern('available_countries*')

        country_text = new_country or texts.t('ADMIN_SERVERS_COUNTRY_DELETED', 'Удален')
        await message.answer(
            texts.t('ADMIN_SERVERS_COUNTRY_UPDATED', '✅ Код страны изменен на: <b>{country}</b>').format(
                country=country_text
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SERVERS_BTN_TO_SERVER', '🔙 К серверу'),
                            callback_data=f'admin_server_edit_{server_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer(texts.t('ADMIN_SERVERS_UPDATE_ERROR', '❌ Ошибка при обновлении сервера'))


@admin_required
@error_handler
async def start_server_edit_limit(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND', '❌ Сервер не найден!'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_limit)

    current_limit = server.max_users or texts.t('ADMIN_SERVERS_NO_LIMIT', 'Без лимита')

    await callback.message.edit_text(
        texts.t(
            'ADMIN_SERVERS_EDIT_LIMIT_PROMPT',
            '👥 <b>Редактирование лимита пользователей</b>\n\n'
            'Текущий лимит: <b>{limit}</b>\n\n'
            'Отправьте новый лимит пользователей (число) или 0 для безлимитного доступа:',
        ).format(limit=current_limit),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_SERVERS_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_server_edit_{server_id}',
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_limit_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    server_id = data.get('server_id')

    try:
        limit = int(message.text.strip())

        if limit < 0:
            await message.answer(texts.t('ADMIN_SERVERS_LIMIT_NEGATIVE', '❌ Лимит не может быть отрицательным'))
            return

        if limit > 10000:
            await message.answer(texts.t('ADMIN_SERVERS_LIMIT_TOO_HIGH', '❌ Слишком большой лимит (максимум 10,000)'))
            return

        max_users = limit if limit > 0 else None

        server = await update_server_squad(db, server_id, max_users=max_users)

        if server:
            await state.clear()

            limit_text = (
                texts.t('ADMIN_SERVERS_LIMIT_USERS', '{count} пользователей').format(count=limit)
                if limit > 0
                else texts.t('ADMIN_SERVERS_NO_LIMIT', 'Без лимита')
            )
            await message.answer(
                texts.t('ADMIN_SERVERS_LIMIT_UPDATED', '✅ Лимит пользователей изменен на: <b>{limit}</b>').format(
                    limit=limit_text
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_SERVERS_BTN_TO_SERVER', '🔙 К серверу'),
                                callback_data=f'admin_server_edit_{server_id}',
                            )
                        ]
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            await message.answer(texts.t('ADMIN_SERVERS_UPDATE_ERROR', '❌ Ошибка при обновлении сервера'))

    except ValueError:
        await message.answer(texts.t('ADMIN_SERVERS_LIMIT_INVALID', '❌ Неверный формат числа. Введите целое число.'))


@admin_required
@error_handler
async def start_server_edit_description(
    callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession
):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND', '❌ Сервер не найден!'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_description)

    current_desc = server.description or texts.t('ADMIN_SERVERS_NOT_SPECIFIED', 'Не указано')

    await callback.message.edit_text(
        texts.t(
            'ADMIN_SERVERS_EDIT_DESC_PROMPT',
            '📝 <b>Редактирование описания</b>\n\n'
            'Текущее описание:\n<i>{desc}</i>\n\n'
            "Отправьте новое описание сервера или '-' для удаления:",
        ).format(desc=current_desc),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_SERVERS_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_server_edit_{server_id}',
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_description_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    server_id = data.get('server_id')

    new_description = message.text.strip()

    if new_description == '-':
        new_description = None
    elif len(new_description) > 1000:
        await message.answer(
            texts.t('ADMIN_SERVERS_DESC_TOO_LONG', '❌ Описание слишком длинное (максимум 1000 символов)')
        )
        return

    server = await update_server_squad(db, server_id, description=new_description)

    if server:
        await state.clear()

        desc_text = new_description or texts.t('ADMIN_SERVERS_DESC_DELETED', 'Удалено')
        await cache.delete_pattern('available_countries*')
        await message.answer(
            texts.t('ADMIN_SERVERS_DESC_UPDATED', '✅ Описание сервера изменено:\n\n<i>{desc}</i>').format(
                desc=desc_text
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SERVERS_BTN_TO_SERVER', '🔙 К серверу'),
                            callback_data=f'admin_server_edit_{server_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer(texts.t('ADMIN_SERVERS_UPDATE_ERROR', '❌ Ошибка при обновлении сервера'))


@admin_required
@error_handler
async def start_server_edit_promo_groups(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND', '❌ Сервер не найден!'), show_alert=True)
        return

    promo_groups_data = await get_promo_groups_with_counts(db)
    promo_groups = [
        {'id': group.id, 'name': group.name, 'is_default': group.is_default} for group, _ in promo_groups_data
    ]

    if not promo_groups:
        await callback.answer(texts.t('ADMIN_SERVERS_NO_PROMO_GROUPS', '❌ Не найдены промогруппы'), show_alert=True)
        return

    selected_ids = {pg.id for pg in server.allowed_promo_groups}
    if not selected_ids:
        default_group = next((pg for pg in promo_groups if pg['is_default']), None)
        if default_group:
            selected_ids.add(default_group['id'])

    await state.set_state(AdminStates.editing_server_promo_groups)
    await state.set_data(
        {
            'server_id': server_id,
            'promo_groups': promo_groups,
            'selected_promo_groups': list(selected_ids),
            'server_name': server.display_name,
        }
    )

    text = texts.t(
        'ADMIN_SERVERS_PROMO_GROUPS_TEXT',
        '🎯 <b>Настройка промогрупп</b>\n\n'
        'Сервер: <b>{name}</b>\n\n'
        'Выберите промогруппы, которым будет доступен этот сервер.\n'
        'Должна быть выбрана минимум одна промогруппа.',
    ).format(name=html.escape(server.display_name))

    await callback.message.edit_text(
        text,
        reply_markup=_build_server_promo_groups_keyboard(server_id, promo_groups, selected_ids),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_server_promo_group(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split('_')
    server_id = int(parts[4])
    group_id = int(parts[5])

    data = await state.get_data()
    if not data or data.get('server_id') != server_id:
        await callback.answer(
            texts.t('ADMIN_SERVERS_PROMO_SESSION_EXPIRED', '⚠️ Сессия редактирования устарела'), show_alert=True
        )
        return

    selected = {int(pg_id) for pg_id in data.get('selected_promo_groups', [])}
    promo_groups = data.get('promo_groups', [])

    if group_id in selected:
        if len(selected) == 1:
            await callback.answer(
                texts.t('ADMIN_SERVERS_PROMO_LAST_GROUP', '⚠️ Нельзя отключить последнюю промогруппу'),
                show_alert=True,
            )
            return
        selected.remove(group_id)
        message = texts.t('ADMIN_SERVERS_PROMO_REMOVED', 'Промогруппа отключена')
    else:
        selected.add(group_id)
        message = texts.t('ADMIN_SERVERS_PROMO_ADDED', 'Промогруппа добавлена')

    await state.update_data(selected_promo_groups=list(selected))

    await callback.message.edit_reply_markup(
        reply_markup=_build_server_promo_groups_keyboard(server_id, promo_groups, selected)
    )
    await callback.answer(message)


@admin_required
@error_handler
async def save_server_promo_groups(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    if not data:
        await callback.answer(texts.t('ADMIN_SERVERS_PROMO_NO_DATA', '⚠️ Нет данных для сохранения'), show_alert=True)
        return

    server_id = data.get('server_id')
    selected = data.get('selected_promo_groups', [])

    if not selected:
        await callback.answer(
            texts.t('ADMIN_SERVERS_PROMO_SELECT_ONE', '❌ Выберите хотя бы одну промогруппу'), show_alert=True
        )
        return

    try:
        server = await update_server_squad_promo_groups(db, server_id, selected)
    except ValueError as exc:
        await callback.answer(f'❌ {exc}', show_alert=True)
        return

    if not server:
        await callback.answer(texts.t('ADMIN_SERVERS_PROMO_SERVER_NOT_FOUND', '❌ Сервер не найден'), show_alert=True)
        return

    await cache.delete_pattern('available_countries*')
    await state.clear()

    text, keyboard = _build_server_edit_view(server)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer(texts.t('ADMIN_SERVERS_PROMO_UPDATED', '✅ Промогруппы обновлены!'))


@admin_required
@error_handler
async def sync_server_user_counts_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_SERVERS_SYNC_COUNTS_PROGRESS', '🔄 Синхронизация счетчиков пользователей...'),
        reply_markup=None,
    )

    try:
        from app.database.crud.server_squad import sync_server_user_counts

        updated_count = await sync_server_user_counts(db)

        text = texts.t(
            'ADMIN_SERVERS_SYNC_COUNTS_DONE',
            '\n✅ <b>Синхронизация завершена</b>\n\n📊 <b>Результат:</b>\n'
            '• Обновлено серверов: {updated_count}\n\n'
            'Счетчики пользователей синхронизированы с реальными данными.\n',
        ).format(updated_count=updated_count)

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SERVERS_BTN_LIST', '📋 Список серверов'),
                    callback_data='admin_servers_list',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SERVERS_BTN_REPEAT', '🔄 Повторить'),
                    callback_data='admin_servers_sync_counts',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SERVERS_BTN_BACK', '⬅️ Назад'),
                    callback_data='admin_servers',
                )
            ],
        ]

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error('Ошибка синхронизации счетчиков', error=e)
        await callback.message.edit_text(
            texts.t('ADMIN_SERVERS_SYNC_COUNTS_ERROR', '❌ Ошибка синхронизации: {error}').format(error=e),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SERVERS_BTN_BACK', '⬅️ Назад'),
                            callback_data='admin_servers',
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def handle_servers_pagination(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    page = int(callback.data.split('_')[-1])
    await show_servers_list(callback, db_user, db, page)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_servers_menu, F.data == 'admin_servers')
    dp.callback_query.register(show_servers_list, F.data == 'admin_servers_list')
    dp.callback_query.register(sync_servers_with_remnawave, F.data == 'admin_servers_sync')
    dp.callback_query.register(sync_server_user_counts_handler, F.data == 'admin_servers_sync_counts')
    dp.callback_query.register(show_server_detailed_stats, F.data == 'admin_servers_stats')

    dp.callback_query.register(
        show_server_edit_menu,
        F.data.startswith('admin_server_edit_')
        & ~F.data.contains('name')
        & ~F.data.contains('price')
        & ~F.data.contains('country')
        & ~F.data.contains('limit')
        & ~F.data.contains('desc')
        & ~F.data.contains('promo'),
    )
    dp.callback_query.register(toggle_server_availability, F.data.startswith('admin_server_toggle_'))
    dp.callback_query.register(toggle_server_trial_assignment, F.data.startswith('admin_server_trial_'))
    dp.callback_query.register(show_server_users, F.data.startswith('admin_server_users_'))

    dp.callback_query.register(start_server_edit_name, F.data.startswith('admin_server_edit_name_'))
    dp.callback_query.register(start_server_edit_price, F.data.startswith('admin_server_edit_price_'))
    dp.callback_query.register(start_server_edit_country, F.data.startswith('admin_server_edit_country_'))
    dp.callback_query.register(start_server_edit_promo_groups, F.data.startswith('admin_server_edit_promo_'))
    dp.callback_query.register(start_server_edit_limit, F.data.startswith('admin_server_edit_limit_'))
    dp.callback_query.register(start_server_edit_description, F.data.startswith('admin_server_edit_desc_'))

    dp.message.register(process_server_name_edit, AdminStates.editing_server_name)
    dp.message.register(process_server_price_edit, AdminStates.editing_server_price)
    dp.message.register(process_server_country_edit, AdminStates.editing_server_country)
    dp.message.register(process_server_limit_edit, AdminStates.editing_server_limit)
    dp.message.register(process_server_description_edit, AdminStates.editing_server_description)
    dp.callback_query.register(toggle_server_promo_group, F.data.startswith('admin_server_promo_toggle_'))
    dp.callback_query.register(save_server_promo_groups, F.data.startswith('admin_server_promo_save_'))

    dp.callback_query.register(
        delete_server_confirm, F.data.startswith('admin_server_delete_') & ~F.data.contains('confirm')
    )
    dp.callback_query.register(delete_server_execute, F.data.startswith('admin_server_delete_confirm_'))

    dp.callback_query.register(handle_servers_pagination, F.data.startswith('admin_servers_list_page_'))
