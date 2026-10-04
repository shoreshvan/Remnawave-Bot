import html
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_group_by_id, get_promo_groups_with_counts
from app.database.crud.promocode import (
    create_promocode,
    delete_promocode,
    get_promocode_by_code,
    get_promocode_by_id,
    get_promocode_statistics,
    get_promocodes_count,
    get_promocodes_list,
    update_promocode,
)
from app.database.models import PromoCodeType, User
from app.keyboards.admin import (
    get_admin_pagination_keyboard,
    get_admin_promocodes_keyboard,
    get_promocode_type_keyboard,
)
from app.localization.texts import get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_promocodes_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    total_codes = await get_promocodes_count(db)
    active_codes = await get_promocodes_count(db, is_active=True)

    text = texts.t(
        'ADMIN_PROMO_MENU_TEXT',
        '\n🎫 <b>Управление промокодами</b>\n\n📊 <b>Статистика:</b>\n'
        '- Всего промокодов: {total}\n- Активных: {active}\n- Неактивных: {inactive}\n\n'
        'Выберите действие:\n',
    ).format(total=total_codes, active=active_codes, inactive=total_codes - active_codes)

    await callback.message.edit_text(text, reply_markup=get_admin_promocodes_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_promocodes_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    texts = get_texts(db_user.language)
    limit = 10
    offset = (page - 1) * limit

    promocodes = await get_promocodes_list(db, offset=offset, limit=limit)
    total_count = await get_promocodes_count(db)
    total_pages = (total_count + limit - 1) // limit

    if not promocodes:
        await callback.message.edit_text(
            texts.t('ADMIN_PROMO_NONE_FOUND', '🎫 Промокоды не найдены'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_PROMO_BACK', '⬅️ Назад'),
                            callback_data='admin_promocodes',
                        )
                    ]
                ]
            ),
        )
        await callback.answer()
        return

    text = texts.t('ADMIN_PROMO_LIST_HEADER', '🎫 <b>Список промокодов</b> (стр. {page}/{total})\n\n').format(
        page=page, total=total_pages
    )
    keyboard = []

    for promo in promocodes:
        status_emoji = '✅' if promo.is_active else '❌'
        type_emoji = {
            'balance': '💰',
            'subscription_days': '📅',
            'trial_subscription': '🎁',
            'promo_group': '🏷️',
            'discount': '💸',
            'balance_and_days': '💰📅',
        }.get(promo.type, '🎫')

        text += f'{status_emoji} {type_emoji} <code>{promo.code}</code>\n'
        text += texts.t('ADMIN_PROMO_LIST_USES', '📊 Использований: {current}/{max}\n').format(
            current=promo.current_uses, max=promo.max_uses
        )

        if promo.type == PromoCodeType.BALANCE.value:
            text += texts.t('ADMIN_PROMO_LIST_BONUS', '💰 Бонус: {amount}\n').format(
                amount=settings.format_price(promo.balance_bonus_kopeks)
            )
        elif promo.type == PromoCodeType.SUBSCRIPTION_DAYS.value:
            text += texts.t('ADMIN_PROMO_LIST_DAYS', '📅 Дней: {days}\n').format(days=promo.subscription_days)
        elif promo.type == PromoCodeType.BALANCE_AND_DAYS.value:
            if promo.balance_bonus_kopeks:
                text += texts.t('ADMIN_PROMO_LIST_BONUS', '💰 Бонус: {amount}\n').format(
                    amount=settings.format_price(promo.balance_bonus_kopeks)
                )
            if promo.subscription_days:
                text += texts.t('ADMIN_PROMO_LIST_DAYS', '📅 Дней: {days}\n').format(days=promo.subscription_days)
            if getattr(promo, 'traffic_gb', 0):
                text += texts.t('ADMIN_PROMO_LIST_TRAFFIC', '📦 Трафик: {traffic} ГБ\n').format(
                    traffic=promo.traffic_gb
                )
        elif promo.type == PromoCodeType.PROMO_GROUP.value:
            if promo.promo_group:
                text += texts.t('ADMIN_PROMO_LIST_GROUP', '🏷️ Промогруппа: {name}\n').format(
                    name=html.escape(promo.promo_group.name)
                )
        elif promo.type == PromoCodeType.DISCOUNT.value:
            discount_hours = promo.subscription_days
            if discount_hours > 0:
                text += texts.t('ADMIN_PROMO_LIST_DISCOUNT_HOURS', '💸 Скидка: {percent}% ({hours} ч.)\n').format(
                    percent=promo.balance_bonus_kopeks, hours=discount_hours
                )
            else:
                text += texts.t('ADMIN_PROMO_LIST_DISCOUNT_BEFORE', '💸 Скидка: {percent}% (до покупки)\n').format(
                    percent=promo.balance_bonus_kopeks
                )

        # Промогруппа комбинируется с любым типом (назначается при активации
        # независимо от type) — показываем прикреплённую группу и у составных
        if promo.type != PromoCodeType.PROMO_GROUP.value and promo.promo_group:
            text += texts.t('ADMIN_PROMO_LIST_GROUP', '🏷️ Промогруппа: {name}\n').format(
                name=html.escape(promo.promo_group.name)
            )

        if promo.valid_until:
            text += texts.t('ADMIN_PROMO_LIST_UNTIL', '⏰ До: {date}\n').format(date=format_datetime(promo.valid_until))

        keyboard.append([types.InlineKeyboardButton(text=f'🎫 {promo.code}', callback_data=f'promo_manage_{promo.id}')])

        text += '\n'

    if total_pages > 1:
        pagination_row = get_admin_pagination_keyboard(
            page, total_pages, 'admin_promo_list', 'admin_promocodes', db_user.language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_CREATE', '➕ Создать'), callback_data='admin_promo_create'
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BACK', '⬅️ Назад'), callback_data='admin_promocodes'
                )
            ],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_promocodes_list_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Обработчик пагинации списка промокодов."""
    try:
        page = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        page = 1
    await show_promocodes_list(callback, db_user, db, page=page)


@admin_required
@error_handler
async def show_promocode_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMO_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    status_emoji = '✅' if promo.is_active else '❌'
    type_emoji = {
        'balance': '💰',
        'subscription_days': '📅',
        'trial_subscription': '🎁',
        'promo_group': '🏷️',
        'discount': '💸',
        'balance_and_days': '💰📅',
    }.get(promo.type, '🎫')

    status_label = (
        texts.t('ADMIN_PROMO_STATUS_ACTIVE', 'Активен')
        if promo.is_active
        else texts.t('ADMIN_PROMO_STATUS_INACTIVE', 'Неактивен')
    )
    text = texts.t(
        'ADMIN_PROMO_MANAGE_HEADER',
        '\n🎫 <b>Управление промокодом</b>\n\n{type_emoji} <b>Код:</b> <code>{code}</code>\n'
        '{status_emoji} <b>Статус:</b> {status}\n📊 <b>Использований:</b> {current}/{max}\n',
    ).format(
        type_emoji=type_emoji,
        code=promo.code,
        status_emoji=status_emoji,
        status=status_label,
        current=promo.current_uses,
        max=promo.max_uses,
    )

    if promo.type == PromoCodeType.BALANCE.value:
        text += texts.t('ADMIN_PROMO_FIELD_BONUS', '💰 <b>Бонус:</b> {amount}\n').format(
            amount=settings.format_price(promo.balance_bonus_kopeks)
        )
    elif promo.type == PromoCodeType.SUBSCRIPTION_DAYS.value:
        text += texts.t('ADMIN_PROMO_FIELD_DAYS', '📅 <b>Дней:</b> {days}\n').format(days=promo.subscription_days)
    elif promo.type == PromoCodeType.BALANCE_AND_DAYS.value:
        if promo.balance_bonus_kopeks:
            text += texts.t('ADMIN_PROMO_FIELD_BONUS', '💰 <b>Бонус:</b> {amount}\n').format(
                amount=settings.format_price(promo.balance_bonus_kopeks)
            )
        if promo.subscription_days:
            text += texts.t('ADMIN_PROMO_FIELD_DAYS', '📅 <b>Дней:</b> {days}\n').format(days=promo.subscription_days)
        if getattr(promo, 'traffic_gb', 0):
            text += texts.t('ADMIN_PROMO_MANAGE_TRAFFIC', '📦 <b>Трафик:</b> {traffic} ГБ\n').format(
                traffic=promo.traffic_gb
            )
    elif promo.type == PromoCodeType.PROMO_GROUP.value:
        if promo.promo_group:
            text += texts.t(
                'ADMIN_PROMO_MANAGE_GROUP_PRIORITY', '🏷️ <b>Промогруппа:</b> {name} (приоритет: {priority})\n'
            ).format(name=html.escape(promo.promo_group.name), priority=promo.promo_group.priority)
        elif promo.promo_group_id:
            text += texts.t(
                'ADMIN_PROMO_MANAGE_GROUP_MISSING', '🏷️ <b>Промогруппа ID:</b> {group_id} (не найдена)\n'
            ).format(group_id=promo.promo_group_id)
    elif promo.type == PromoCodeType.DISCOUNT.value:
        discount_hours = promo.subscription_days
        if discount_hours > 0:
            text += texts.t(
                'ADMIN_PROMO_MANAGE_DISCOUNT_HOURS', '💸 <b>Скидка:</b> {percent}% (срок: {hours} ч.)\n'
            ).format(percent=promo.balance_bonus_kopeks, hours=discount_hours)
        else:
            text += texts.t(
                'ADMIN_PROMO_MANAGE_DISCOUNT_BEFORE', '💸 <b>Скидка:</b> {percent}% (до первой покупки)\n'
            ).format(percent=promo.balance_bonus_kopeks)

    # Промогруппа комбинируется с любым типом (назначается при активации
    # независимо от type) — показываем прикреплённую группу и у составных
    if promo.type != PromoCodeType.PROMO_GROUP.value and promo.promo_group:
        text += texts.t('ADMIN_PROMO_FIELD_GROUP', '🏷️ <b>Промогруппа:</b> {name}\n').format(
            name=html.escape(promo.promo_group.name)
        )

    if promo.valid_until:
        text += texts.t('ADMIN_PROMO_FIELD_VALID_UNTIL', '⏰ <b>Действует до:</b> {date}\n').format(
            date=format_datetime(promo.valid_until)
        )

    first_purchase_only = getattr(promo, 'first_purchase_only', False)
    first_purchase_emoji = '✅' if first_purchase_only else '❌'
    text += texts.t('ADMIN_PROMO_MANAGE_FIRST_PURCHASE', '🆕 <b>Только первая покупка:</b> {emoji}\n').format(
        emoji=first_purchase_emoji
    )

    text += texts.t('ADMIN_PROMO_MANAGE_CREATED', '📅 <b>Создан:</b> {date}\n').format(
        date=format_datetime(promo.created_at)
    )

    first_purchase_btn_text = texts.t('ADMIN_PROMO_BTN_FIRST_PURCHASE', '🆕 Первая покупка: {emoji}').format(
        emoji='✅' if first_purchase_only else '❌'
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMO_BTN_EDIT', '✏️ Редактировать'), callback_data=f'promo_edit_{promo.id}'
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMO_BTN_TOGGLE_STATUS', '🔄 Переключить статус'),
                callback_data=f'promo_toggle_{promo.id}',
            ),
        ],
        [types.InlineKeyboardButton(text=first_purchase_btn_text, callback_data=f'promo_toggle_first_{promo.id}')],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMO_BTN_STATS', '📊 Статистика'), callback_data=f'promo_stats_{promo.id}'
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMO_BTN_DELETE', '🗑️ Удалить'), callback_data=f'promo_delete_{promo.id}'
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMO_BTN_TO_LIST', '⬅️ К списку'), callback_data='admin_promo_list'
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_promocode_edit_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMO_ERROR_GET_ID', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMO_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    text = texts.t(
        'ADMIN_PROMO_EDIT_MENU_HEADER',
        '\n✏️ <b>Редактирование промокода</b> <code>{code}</code>\n\n💰 <b>Текущие параметры:</b>\n',
    ).format(code=promo.code)

    if promo.type == PromoCodeType.BALANCE.value:
        text += texts.t('ADMIN_PROMO_EDIT_PARAM_BONUS', '• Бонус: {amount}\n').format(
            amount=settings.format_price(promo.balance_bonus_kopeks)
        )
    elif promo.type in [PromoCodeType.SUBSCRIPTION_DAYS.value, PromoCodeType.TRIAL_SUBSCRIPTION.value]:
        text += texts.t('ADMIN_PROMO_EDIT_PARAM_DAYS', '• Дней: {days}\n').format(days=promo.subscription_days)
    elif promo.type == PromoCodeType.BALANCE_AND_DAYS.value:
        if promo.balance_bonus_kopeks:
            text += texts.t('ADMIN_PROMO_EDIT_PARAM_BONUS', '• Бонус: {amount}\n').format(
                amount=settings.format_price(promo.balance_bonus_kopeks)
            )
        if promo.subscription_days:
            text += texts.t('ADMIN_PROMO_EDIT_PARAM_DAYS', '• Дней: {days}\n').format(days=promo.subscription_days)
        if getattr(promo, 'traffic_gb', 0):
            text += texts.t('ADMIN_PROMO_EDIT_PARAM_TRAFFIC', '• Трафик: {traffic} ГБ\n').format(
                traffic=promo.traffic_gb
            )

    text += texts.t('ADMIN_PROMO_EDIT_PARAM_USES', '• Использований: {current}/{max}\n').format(
        current=promo.current_uses, max=promo.max_uses
    )

    if promo.valid_until:
        text += texts.t('ADMIN_PROMO_EDIT_PARAM_UNTIL', '• До: {date}\n').format(
            date=format_datetime(promo.valid_until)
        )
    else:
        text += texts.t('ADMIN_PROMO_EDIT_PARAM_UNLIMITED', '• Срок: бессрочно\n')

    text += texts.t('ADMIN_PROMO_EDIT_CHOOSE_PARAM', '\nВыберите параметр для изменения:')

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMO_BTN_EDIT_DATE', '📅 Дата окончания'),
                callback_data=f'promo_edit_date_{promo.id}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PROMO_BTN_EDIT_USES', '📊 Количество использований'),
                callback_data=f'promo_edit_uses_{promo.id}',
            )
        ],
    ]

    if promo.type == PromoCodeType.BALANCE.value:
        keyboard.insert(
            1,
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_EDIT_AMOUNT', '💰 Сумма бонуса'),
                    callback_data=f'promo_edit_amount_{promo.id}',
                )
            ],
        )
    elif promo.type in [PromoCodeType.SUBSCRIPTION_DAYS.value, PromoCodeType.TRIAL_SUBSCRIPTION.value]:
        keyboard.insert(
            1,
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_EDIT_DAYS', '📅 Количество дней'),
                    callback_data=f'promo_edit_days_{promo.id}',
                )
            ],
        )
    elif promo.type == PromoCodeType.BALANCE_AND_DAYS.value:
        keyboard.insert(
            1,
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_EDIT_AMOUNT', '💰 Сумма бонуса'),
                    callback_data=f'promo_edit_amount_{promo.id}',
                )
            ],
        )
        keyboard.insert(
            2,
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_EDIT_DAYS', '📅 Количество дней'),
                    callback_data=f'promo_edit_days_{promo.id}',
                )
            ],
        )

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BACK', '⬅️ Назад'), callback_data=f'promo_manage_{promo.id}'
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_date(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMO_ERROR_GET_ID', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='date')

    text = texts.t(
        'ADMIN_PROMO_EDIT_DATE_PROMPT',
        '\n📅 <b>Изменение даты окончания промокода</b>\n\n'
        'Введите количество дней до окончания (от текущего момента):\n'
        '• Введите <b>0</b> для бессрочного промокода\n'
        '• Введите положительное число для установки срока\n\n'
        '<i>Например: 30 (промокод будет действовать 30 дней)</i>\n\n'
        'ID промокода: {promo_id}\n',
    ).format(promo_id=promo_id)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_CANCEL', '❌ Отмена'), callback_data=f'promo_edit_{promo_id}'
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_expiry)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_amount(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMO_ERROR_GET_ID', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='amount')

    text = texts.t(
        'ADMIN_PROMO_EDIT_AMOUNT_PROMPT',
        '\n💰 <b>Изменение суммы бонуса промокода</b>\n\n'
        'Введите новую сумму в تومان:\n<i>Например: 500</i>\n\n'
        'ID промокода: {promo_id}\n',
    ).format(promo_id=promo_id)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_CANCEL', '❌ Отмена'), callback_data=f'promo_edit_{promo_id}'
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_value)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_days(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    # ИСПРАВЛЕНИЕ: берем последний элемент как ID
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMO_ERROR_GET_ID', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='days')

    text = texts.t(
        'ADMIN_PROMO_EDIT_DAYS_PROMPT',
        '\n📅 <b>Изменение количества дней подписки</b>\n\n'
        'Введите новое количество дней:\n<i>Например: 30</i>\n\n'
        'ID промокода: {promo_id}\n',
    ).format(promo_id=promo_id)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_CANCEL', '❌ Отмена'), callback_data=f'promo_edit_{promo_id}'
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_value)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_uses(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMO_ERROR_GET_ID', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='uses')

    text = texts.t(
        'ADMIN_PROMO_EDIT_USES_PROMPT',
        '\n📊 <b>Изменение максимального количества использований</b>\n\n'
        'Введите новое количество использований:\n'
        '• Введите <b>0</b> для безлимитных использований\n'
        '• Введите положительное число для ограничения\n\n'
        '<i>Например: 100</i>\n\n'
        'ID промокода: {promo_id}\n',
    ).format(promo_id=promo_id)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_CANCEL', '❌ Отмена'), callback_data=f'promo_edit_{promo_id}'
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_uses)
    await callback.answer()


@admin_required
@error_handler
async def start_promocode_creation(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_PROMO_CREATE_CHOOSE_TYPE', '🎫 <b>Создание промокода</b>\n\nВыберите тип промокода:'),
        reply_markup=get_promocode_type_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def select_promocode_type(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    promo_type = callback.data.split('_')[-1]

    type_names = {
        'balance': texts.t('ADMIN_PROMO_TYPE_BALANCE', '💰 Пополнение баланса'),
        'days': texts.t('ADMIN_PROMO_TYPE_DAYS', '📅 Дни подписки'),
        'trial': texts.t('ADMIN_PROMO_TYPE_TRIAL', '🎁 Тестовая подписка'),
        'group': texts.t('ADMIN_PROMO_TYPE_GROUP', '🏷️ Промогруппа'),
        'discount': texts.t('ADMIN_PROMO_TYPE_DISCOUNT', '💸 Одноразовая скидка'),
        'combo': texts.t('ADMIN_PROMO_TYPE_COMBO', '💰📅 Баланс + дни подписки'),
    }

    await state.update_data(promocode_type=promo_type)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_PROMO_CREATE_ENTER_CODE',
            '🎫 <b>Создание промокода</b>\n\nТип: {type}\n\n'
            'Введите код промокода (только латинские буквы и цифры):',
        ).format(type=type_names.get(promo_type, promo_type)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_PROMO_BTN_CANCEL', '❌ Отмена'), callback_data='admin_promocodes'
                    )
                ]
            ]
        ),
    )

    await state.set_state(AdminStates.creating_promocode)
    await callback.answer()


@admin_required
@error_handler
async def process_promocode_code(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    code = message.text.strip().upper()

    if not code.isalnum() or len(code) < 3 or len(code) > 20:
        await message.answer(
            texts.t(
                'ADMIN_PROMO_ERROR_CODE_FORMAT',
                '❌ Код должен содержать только латинские буквы и цифры (3-20 символов)',
            )
        )
        return

    existing = await get_promocode_by_code(db, code)
    if existing:
        await message.answer(texts.t('ADMIN_PROMO_ERROR_CODE_EXISTS', '❌ Промокод с таким кодом уже существует'))
        return

    await state.update_data(promocode_code=code)

    data = await state.get_data()
    promo_type = data.get('promocode_type')

    if promo_type == 'balance':
        await message.answer(
            texts.t(
                'ADMIN_PROMO_ENTER_BALANCE',
                '💰 <b>Промокод:</b> <code>{code}</code>\n\nВведите сумму пополнения баланса (в تومان):',
            ).format(code=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'combo':
        await message.answer(
            texts.t(
                'ADMIN_PROMO_ENTER_COMBO_STEP1',
                '💰📅 <b>Промокод:</b> <code>{code}</code>\n\n'
                'Шаг 1 из 2: введите сумму пополнения баланса (в تومان), '
                'дни подписки спрошу следующим шагом:',
            ).format(code=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'days':
        await message.answer(
            texts.t(
                'ADMIN_PROMO_ENTER_DAYS',
                '📅 <b>Промокод:</b> <code>{code}</code>\n\nВведите количество дней подписки:',
            ).format(code=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'trial':
        await message.answer(
            texts.t(
                'ADMIN_PROMO_ENTER_TRIAL_DAYS',
                '🎁 <b>Промокод:</b> <code>{code}</code>\n\nВведите количество дней тестовой подписки:',
            ).format(code=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'discount':
        await message.answer(
            texts.t(
                'ADMIN_PROMO_ENTER_DISCOUNT_PERCENT',
                '💸 <b>Промокод:</b> <code>{code}</code>\n\nВведите процент скидки (1-100):',
            ).format(code=code)
        )
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'group':
        # Show promo group selection
        groups_with_counts = await get_promo_groups_with_counts(db, limit=50)

        if not groups_with_counts:
            await message.answer(
                texts.t('ADMIN_PROMO_ERROR_NO_GROUPS', '❌ Промогруппы не найдены. Создайте хотя бы одну промогруппу.'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_PROMO_BACK', '⬅️ Назад'), callback_data='admin_promocodes'
                            )
                        ]
                    ]
                ),
            )
            await state.clear()
            return

        keyboard = []
        text = texts.t(
            'ADMIN_PROMO_SELECT_GROUP_HEADER',
            '🏷️ <b>Промокод:</b> <code>{code}</code>\n\nВыберите промогруппу для назначения:\n\n',
        ).format(code=code)

        for promo_group, user_count in groups_with_counts:
            text += texts.t(
                'ADMIN_PROMO_GROUP_LINE', '• {name} (приоритет: {priority}, пользователей: {users})\n'
            ).format(name=html.escape(promo_group.name), priority=promo_group.priority, users=user_count)
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'{promo_group.name} (↑{promo_group.priority})',
                        callback_data=f'promo_select_group_{promo_group.id}',
                    )
                ]
            )

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_CANCEL', '❌ Отмена'), callback_data='admin_promocodes'
                )
            ]
        )

        await message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
        await state.set_state(AdminStates.selecting_promo_group)


@admin_required
@error_handler
async def process_promo_group_selection(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession
):
    """Handle promo group selection for promocode"""
    texts = get_texts(db_user.language)
    try:
        promo_group_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(
            texts.t('ADMIN_PROMO_ERROR_GET_GROUP_ID', '❌ Ошибка получения ID промогруппы'), show_alert=True
        )
        return

    promo_group = await get_promo_group_by_id(db, promo_group_id)
    if not promo_group:
        await callback.answer(texts.t('ADMIN_PROMO_ERROR_GROUP_NOT_FOUND', '❌ Промогруппа не найдена'), show_alert=True)
        return

    await state.update_data(promo_group_id=promo_group_id, promo_group_name=promo_group.name)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_PROMO_GROUP_SELECTED',
            '🏷️ <b>Промокод для промогруппы</b>\n\nПромогруппа: {name}\nПриоритет: {priority}\n\n'
            '📊 Введите количество использований промокода (или 0 для безлимита):',
        ).format(name=html.escape(promo_group.name), priority=promo_group.priority)
    )

    await state.set_state(AdminStates.setting_promocode_uses)
    await callback.answer()


@admin_required
@error_handler
async def process_promocode_value(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()

    if data.get('editing_promo_id'):
        await handle_edit_value(message, db_user, state, db)
        return

    try:
        value = int(message.text.strip())

        promo_type = data.get('promocode_type')

        if promo_type in ['balance', 'combo'] and (value < 1 or value > 10000):
            await message.answer(texts.t('ADMIN_PROMO_ERROR_AMOUNT_RANGE', '❌ Сумма должна быть от 1 до 10,000 تومان'))
            return
        if promo_type in ['days', 'trial'] and (value < 1 or value > 3650):
            await message.answer(texts.t('ADMIN_PROMO_ERROR_DAYS_RANGE', '❌ Количество дней должно быть от 1 до 3650'))
            return
        if promo_type == 'discount' and (value < 1 or value > 100):
            await message.answer(
                texts.t('ADMIN_PROMO_ERROR_DISCOUNT_RANGE', '❌ Процент скидки должен быть от 1 до 100')
            )
            return

        await state.update_data(promocode_value=value)

        # Для комбинированного типа сумма — только первый шаг, дальше дни
        if promo_type == 'combo':
            await message.answer(
                texts.t('ADMIN_PROMO_ENTER_COMBO_STEP2', '📅 Шаг 2 из 2: введите количество дней подписки:')
            )
            await state.set_state(AdminStates.setting_promocode_combo_days)
            return

        await message.answer(
            texts.t('ADMIN_PROMO_ENTER_USES', '📊 Введите количество использований промокода (или 0 для безлимита):')
        )
        await state.set_state(AdminStates.setting_promocode_uses)

    except ValueError:
        await message.answer(texts.t('ADMIN_PROMO_ERROR_INVALID_NUMBER', '❌ Введите корректное число'))


@admin_required
@error_handler
async def process_promocode_combo_days(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    """Шаг 2 комбинированного промокода (BALANCE_AND_DAYS): ввод дней подписки."""
    texts = get_texts(db_user.language)
    try:
        days = int(message.text.strip())

        if days < 1 or days > 3650:
            await message.answer(texts.t('ADMIN_PROMO_ERROR_DAYS_RANGE', '❌ Количество дней должно быть от 1 до 3650'))
            return

        await state.update_data(promocode_combo_days=days)

        await message.answer(
            texts.t('ADMIN_PROMO_ENTER_USES', '📊 Введите количество использований промокода (или 0 для безлимита):')
        )
        await state.set_state(AdminStates.setting_promocode_uses)

    except ValueError:
        await message.answer(texts.t('ADMIN_PROMO_ERROR_INVALID_DAYS', '❌ Введите корректное число дней'))


async def handle_edit_value(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')
    edit_action = data.get('edit_action')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer(texts.t('ADMIN_PROMO_NOT_FOUND', '❌ Промокод не найден'))
        await state.clear()
        return

    try:
        value = int(message.text.strip())

        if edit_action == 'amount':
            if value < 1 or value > 10000:
                await message.answer(
                    texts.t('ADMIN_PROMO_ERROR_AMOUNT_RANGE', '❌ Сумма должна быть от 1 до 10,000 تومان')
                )
                return

            await update_promocode(db, promo, balance_bonus_kopeks=value * 100)
            await message.answer(
                texts.t('ADMIN_PROMO_AMOUNT_CHANGED', '✅ Сумма бонуса изменена на {value} تومان').format(value=value),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_PROMO_BTN_TO_PROMO', '🎫 К промокоду'),
                                callback_data=f'promo_manage_{promo_id}',
                            )
                        ]
                    ]
                ),
            )

        elif edit_action == 'days':
            if value < 1 or value > 3650:
                await message.answer(
                    texts.t('ADMIN_PROMO_ERROR_DAYS_RANGE', '❌ Количество дней должно быть от 1 до 3650')
                )
                return

            await update_promocode(db, promo, subscription_days=value)
            await message.answer(
                texts.t('ADMIN_PROMO_DAYS_CHANGED', '✅ Количество дней изменено на {value}').format(value=value),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_PROMO_BTN_TO_PROMO', '🎫 К промокоду'),
                                callback_data=f'promo_manage_{promo_id}',
                            )
                        ]
                    ]
                ),
            )

        await state.clear()
        logger.info(
            'Промокод отредактирован администратором',
            code=promo.code,
            telegram_id=db_user.telegram_id,
            edit_action=edit_action,
            value=value,
        )

    except ValueError:
        await message.answer(texts.t('ADMIN_PROMO_ERROR_INVALID_NUMBER', '❌ Введите корректное число'))


@admin_required
@error_handler
async def process_promocode_uses(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()

    if data.get('editing_promo_id'):
        await handle_edit_uses(message, db_user, state, db)
        return

    try:
        max_uses = int(message.text.strip())

        if max_uses < 0 or max_uses > 100000:
            await message.answer(
                texts.t('ADMIN_PROMO_ERROR_USES_RANGE', '❌ Количество использований должно быть от 0 до 100,000')
            )
            return

        if max_uses == 0:
            max_uses = 999999

        await state.update_data(promocode_max_uses=max_uses)

        await message.answer(
            texts.t('ADMIN_PROMO_ENTER_EXPIRY', '⏰ Введите срок действия промокода в днях (или 0 для бессрочного):')
        )
        await state.set_state(AdminStates.setting_promocode_expiry)

    except ValueError:
        await message.answer(texts.t('ADMIN_PROMO_ERROR_INVALID_NUMBER', '❌ Введите корректное число'))


async def handle_edit_uses(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer(texts.t('ADMIN_PROMO_NOT_FOUND', '❌ Промокод не найден'))
        await state.clear()
        return

    try:
        max_uses = int(message.text.strip())

        if max_uses < 0 or max_uses > 100000:
            await message.answer(
                texts.t('ADMIN_PROMO_ERROR_USES_RANGE', '❌ Количество использований должно быть от 0 до 100,000')
            )
            return

        if max_uses == 0:
            max_uses = 999999

        if max_uses < promo.current_uses:
            await message.answer(
                texts.t(
                    'ADMIN_PROMO_ERROR_LIMIT_TOO_LOW',
                    '❌ Новый лимит ({limit}) не может быть меньше текущих использований ({current})',
                ).format(limit=max_uses, current=promo.current_uses)
            )
            return

        await update_promocode(db, promo, max_uses=max_uses)

        uses_text = (
            texts.t('ADMIN_PROMO_UNLIMITED_USES', 'безлимитное') if max_uses == 999999 else str(max_uses)
        )
        await message.answer(
            texts.t('ADMIN_PROMO_USES_CHANGED', '✅ Максимальное количество использований изменено на {uses}').format(
                uses=uses_text
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_PROMO_BTN_TO_PROMO', '🎫 К промокоду'),
                            callback_data=f'promo_manage_{promo_id}',
                        )
                    ]
                ]
            ),
        )

        await state.clear()
        logger.info(
            'Промокод отредактирован администратором max_uses',
            code=promo.code,
            telegram_id=db_user.telegram_id,
            max_uses=max_uses,
        )

    except ValueError:
        await message.answer(texts.t('ADMIN_PROMO_ERROR_INVALID_NUMBER', '❌ Введите корректное число'))


@admin_required
@error_handler
async def process_promocode_expiry(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()

    if data.get('editing_promo_id'):
        await handle_edit_expiry(message, db_user, state, db)
        return

    try:
        expiry_days = int(message.text.strip())

        if expiry_days < 0 or expiry_days > 3650:
            await message.answer(
                texts.t('ADMIN_PROMO_ERROR_EXPIRY_RANGE', '❌ Срок действия должен быть от 0 до 3650 дней')
            )
            return

        code = data.get('promocode_code')
        promo_type = data.get('promocode_type')
        value = data.get('promocode_value', 0)
        max_uses = data.get('promocode_max_uses', 1)
        promo_group_id = data.get('promo_group_id')
        promo_group_name = data.get('promo_group_name')

        # Для DISCOUNT типа нужно дополнительно спросить срок действия скидки в часах
        if promo_type == 'discount':
            await state.update_data(promocode_expiry_days=expiry_days)
            await message.answer(
                texts.t(
                    'ADMIN_PROMO_ENTER_DISCOUNT_HOURS',
                    '⏰ <b>Промокод:</b> <code>{code}</code>\n\n'
                    'Введите срок действия скидки в часах (0-8760):\n'
                    '0 = бессрочно до первой покупки',
                ).format(code=code)
            )
            await state.set_state(AdminStates.setting_discount_hours)
            return

        valid_until = None
        if expiry_days > 0:
            valid_until = datetime.now(UTC) + timedelta(days=expiry_days)

        type_map = {
            'balance': PromoCodeType.BALANCE,
            'days': PromoCodeType.SUBSCRIPTION_DAYS,
            'trial': PromoCodeType.TRIAL_SUBSCRIPTION,
            'group': PromoCodeType.PROMO_GROUP,
            'combo': PromoCodeType.BALANCE_AND_DAYS,
        }

        if promo_type == 'combo':
            balance_bonus_kopeks = value * 100
            subscription_days = data.get('promocode_combo_days', 0)
        else:
            balance_bonus_kopeks = value * 100 if promo_type == 'balance' else 0
            subscription_days = value if promo_type in ['days', 'trial'] else 0

        promocode = await create_promocode(
            db=db,
            code=code,
            type=type_map[promo_type],
            balance_bonus_kopeks=balance_bonus_kopeks,
            subscription_days=subscription_days,
            max_uses=max_uses,
            valid_until=valid_until,
            created_by=db_user.id,
            promo_group_id=promo_group_id if promo_type == 'group' else None,
        )

        type_names = {
            'balance': texts.t('ADMIN_PROMO_TYPE_NAME_BALANCE', 'Пополнение баланса'),
            'days': texts.t('ADMIN_PROMO_TYPE_NAME_DAYS', 'Дни подписки'),
            'trial': texts.t('ADMIN_PROMO_TYPE_NAME_TRIAL', 'Тестовая подписка'),
            'group': texts.t('ADMIN_PROMO_TYPE_NAME_GROUP', 'Промогруппа'),
            'combo': texts.t('ADMIN_PROMO_TYPE_NAME_COMBO', 'Баланс + дни подписки'),
        }

        summary_text = texts.t(
            'ADMIN_PROMO_CREATED_HEADER',
            '\n✅ <b>Промокод создан!</b>\n\n🎫 <b>Код:</b> <code>{code}</code>\n📝 <b>Тип:</b> {type}\n',
        ).format(code=promocode.code, type=type_names.get(promo_type))

        if promo_type == 'balance':
            summary_text += texts.t('ADMIN_PROMO_FIELD_AMOUNT', '💰 <b>Сумма:</b> {amount}\n').format(
                amount=settings.format_price(promocode.balance_bonus_kopeks)
            )
        elif promo_type in ['days', 'trial']:
            summary_text += texts.t('ADMIN_PROMO_FIELD_DAYS', '📅 <b>Дней:</b> {days}\n').format(
                days=promocode.subscription_days
            )
        elif promo_type == 'combo':
            summary_text += texts.t('ADMIN_PROMO_FIELD_AMOUNT', '💰 <b>Сумма:</b> {amount}\n').format(
                amount=settings.format_price(promocode.balance_bonus_kopeks)
            )
            summary_text += texts.t('ADMIN_PROMO_FIELD_DAYS', '📅 <b>Дней:</b> {days}\n').format(
                days=promocode.subscription_days
            )
        elif promo_type == 'group' and promo_group_name:
            summary_text += texts.t('ADMIN_PROMO_SUMMARY_GROUP', '🏷️ <b>Промогруппа:</b> {group}\n').format(
                group=promo_group_name
            )

        summary_text += texts.t('ADMIN_PROMO_FIELD_USES_MAX', '📊 <b>Использований:</b> {max}\n').format(
            max=promocode.max_uses
        )

        if promocode.valid_until:
            summary_text += texts.t('ADMIN_PROMO_FIELD_VALID_UNTIL', '⏰ <b>Действует до:</b> {date}\n').format(
                date=format_datetime(promocode.valid_until)
            )

        await message.answer(
            summary_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_PROMO_BTN_TO_PROMOS', '🎫 К промокодам'),
                            callback_data='admin_promocodes',
                        )
                    ]
                ]
            ),
        )

        await state.clear()
        logger.info('Создан промокод администратором', code=code, telegram_id=db_user.telegram_id)

    except ValueError:
        await message.answer(texts.t('ADMIN_PROMO_ERROR_INVALID_DAYS', '❌ Введите корректное число дней'))


@admin_required
@error_handler
async def process_discount_hours(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    """Обработчик ввода срока действия скидки в часах для DISCOUNT промокода."""
    texts = get_texts(db_user.language)
    data = await state.get_data()

    try:
        discount_hours = int(message.text.strip())

        if discount_hours < 0 or discount_hours > 8760:
            await message.answer(
                texts.t(
                    'ADMIN_PROMO_ERROR_DISCOUNT_HOURS_RANGE',
                    '❌ Срок действия скидки должен быть от 0 до 8760 часов',
                )
            )
            return

        code = data.get('promocode_code')
        value = data.get('promocode_value', 0)  # Процент скидки
        max_uses = data.get('promocode_max_uses', 1)
        expiry_days = data.get('promocode_expiry_days', 0)

        valid_until = None
        if expiry_days > 0:
            valid_until = datetime.now(UTC) + timedelta(days=expiry_days)

        # Создаем DISCOUNT промокод
        # balance_bonus_kopeks = процент скидки (НЕ копейки!)
        # subscription_days = срок действия скидки в часах (НЕ дни!)
        promocode = await create_promocode(
            db=db,
            code=code,
            type=PromoCodeType.DISCOUNT,
            balance_bonus_kopeks=value,  # Процент (1-100)
            subscription_days=discount_hours,  # Часы (0-8760)
            max_uses=max_uses,
            valid_until=valid_until,
            created_by=db_user.id,
            promo_group_id=None,
        )

        summary_text = texts.t(
            'ADMIN_PROMO_CREATED_DISCOUNT_HEADER',
            '\n✅ <b>Промокод создан!</b>\n\n🎫 <b>Код:</b> <code>{code}</code>\n'
            '📝 <b>Тип:</b> Одноразовая скидка\n💸 <b>Скидка:</b> {discount}%\n',
        ).format(code=promocode.code, discount=promocode.balance_bonus_kopeks)

        if discount_hours > 0:
            summary_text += texts.t(
                'ADMIN_PROMO_CREATED_DISCOUNT_TERM_HOURS', '⏰ <b>Срок скидки:</b> {hours} ч.\n'
            ).format(hours=discount_hours)
        else:
            summary_text += texts.t(
                'ADMIN_PROMO_CREATED_DISCOUNT_TERM_BEFORE', '⏰ <b>Срок скидки:</b> до первой покупки\n'
            )

        summary_text += texts.t('ADMIN_PROMO_FIELD_USES_MAX', '📊 <b>Использований:</b> {max}\n').format(
            max=promocode.max_uses
        )

        if promocode.valid_until:
            summary_text += texts.t(
                'ADMIN_PROMO_CREATED_VALID_UNTIL', '⏳ <b>Промокод действует до:</b> {date}\n'
            ).format(date=format_datetime(promocode.valid_until))

        await message.answer(
            summary_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_PROMO_BTN_TO_PROMOS', '🎫 К промокодам'),
                            callback_data='admin_promocodes',
                        )
                    ]
                ]
            ),
        )

        await state.clear()
        logger.info(
            'Создан DISCOUNT промокод (%, ч) администратором',
            code=code,
            value=value,
            discount_hours=discount_hours,
            telegram_id=db_user.telegram_id,
        )

    except ValueError:
        await message.answer(texts.t('ADMIN_PROMO_ERROR_INVALID_HOURS', '❌ Введите корректное число часов'))


async def handle_edit_expiry(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer(texts.t('ADMIN_PROMO_NOT_FOUND', '❌ Промокод не найден'))
        await state.clear()
        return

    try:
        expiry_days = int(message.text.strip())

        if expiry_days < 0 or expiry_days > 3650:
            await message.answer(
                texts.t('ADMIN_PROMO_ERROR_EXPIRY_RANGE', '❌ Срок действия должен быть от 0 до 3650 дней')
            )
            return

        valid_until = None
        if expiry_days > 0:
            valid_until = datetime.now(UTC) + timedelta(days=expiry_days)

        await update_promocode(db, promo, valid_until=valid_until)

        if valid_until:
            expiry_text = texts.t('ADMIN_PROMO_EXPIRY_UNTIL', 'до {date}').format(date=format_datetime(valid_until))
        else:
            expiry_text = texts.t('ADMIN_PROMO_EXPIRY_UNLIMITED', 'бессрочно')

        await message.answer(
            texts.t('ADMIN_PROMO_EXPIRY_CHANGED', '✅ Срок действия промокода изменен: {expiry}').format(
                expiry=expiry_text
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_PROMO_BTN_TO_PROMO', '🎫 К промокоду'),
                            callback_data=f'promo_manage_{promo_id}',
                        )
                    ]
                ]
            ),
        )

        await state.clear()
        logger.info(
            'Промокод отредактирован администратором expiry дней',
            code=promo.code,
            telegram_id=db_user.telegram_id,
            expiry_days=expiry_days,
        )

    except ValueError:
        await message.answer(texts.t('ADMIN_PROMO_ERROR_INVALID_DAYS', '❌ Введите корректное число дней'))


@admin_required
@error_handler
async def toggle_promocode_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMO_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    new_status = not promo.is_active
    await update_promocode(db, promo, is_active=new_status)

    status_text = (
        texts.t('ADMIN_PROMO_ACTIVATED', 'активирован')
        if new_status
        else texts.t('ADMIN_PROMO_DEACTIVATED', 'деактивирован')
    )
    await callback.answer(
        texts.t('ADMIN_PROMO_STATUS_TOGGLED', '✅ Промокод {status}').format(status=status_text), show_alert=True
    )

    await show_promocode_management(callback, db_user, db)


@admin_required
@error_handler
async def toggle_promocode_first_purchase(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Переключает режим 'только для первой покупки'."""
    texts = get_texts(db_user.language)
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMO_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    new_status = not getattr(promo, 'first_purchase_only', False)
    await update_promocode(db, promo, first_purchase_only=new_status)

    status_text = (
        texts.t('ADMIN_PROMO_ENABLED', 'включён') if new_status else texts.t('ADMIN_PROMO_DISABLED', 'выключен')
    )
    await callback.answer(
        texts.t('ADMIN_PROMO_FIRST_PURCHASE_TOGGLED', "✅ Режим 'первая покупка' {status}").format(
            status=status_text
        ),
        show_alert=True,
    )

    await show_promocode_management(callback, db_user, db)


@admin_required
@error_handler
async def confirm_delete_promocode(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMO_ERROR_GET_ID', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMO_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    status = (
        texts.t('ADMIN_PROMO_STATUS_ACTIVE', 'Активен')
        if promo.is_active
        else texts.t('ADMIN_PROMO_STATUS_INACTIVE', 'Неактивен')
    )
    text = texts.t(
        'ADMIN_PROMO_DELETE_CONFIRM',
        '\n⚠️ <b>Подтверждение удаления</b>\n\n'
        'Вы действительно хотите удалить промокод <code>{code}</code>?\n\n'
        '📊 <b>Информация о промокоде:</b>\n'
        '• Использований: {current}/{max}\n'
        '• Статус: {status}\n\n'
        '<b>⚠️ Внимание:</b> Это действие нельзя отменить!\n\n'
        'ID: {promo_id}\n',
    ).format(code=promo.code, current=promo.current_uses, max=promo.max_uses, status=status, promo_id=promo_id)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_CONFIRM_DELETE', '✅ Да, удалить'),
                    callback_data=f'promo_delete_confirm_{promo.id}',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_CANCEL', '❌ Отмена'),
                    callback_data=f'promo_manage_{promo.id}',
                ),
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def delete_promocode_confirmed(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_PROMO_ERROR_GET_ID', '❌ Ошибка получения ID промокода'), show_alert=True)
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMO_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    code = promo.code
    success = await delete_promocode(db, promo)

    if success:
        await callback.answer(
            texts.t('ADMIN_PROMO_DELETED', '✅ Промокод {code} удален').format(code=code), show_alert=True
        )
        await show_promocodes_list(callback, db_user, db)
    else:
        await callback.answer(texts.t('ADMIN_PROMO_ERROR_DELETE', '❌ Ошибка удаления промокода'), show_alert=True)


@admin_required
@error_handler
async def show_promocode_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer(texts.t('ADMIN_PROMO_NOT_FOUND', '❌ Промокод не найден'), show_alert=True)
        return

    stats = await get_promocode_statistics(db, promo_id)

    text = texts.t(
        'ADMIN_PROMO_STATS_HEADER',
        '\n📊 <b>Статистика промокода</b> <code>{code}</code>\n\n'
        '📈 <b>Общая статистика:</b>\n'
        '- Всего использований: {total}\n'
        '- Использований сегодня: {today}\n'
        '- Осталось использований: {remaining}\n\n'
        '📅 <b>Последние использования:</b>\n',
    ).format(
        code=promo.code,
        total=stats['total_uses'],
        today=stats['today_uses'],
        remaining=promo.max_uses - promo.current_uses,
    )

    if stats['recent_uses']:
        for use in stats['recent_uses'][:5]:
            use_date = format_datetime(use.used_at)

            if hasattr(use, 'user_username') and use.user_username:
                user_display = f'@{html.escape(use.user_username)}'
            elif hasattr(use, 'user_full_name') and use.user_full_name:
                user_display = html.escape(use.user_full_name)
            elif hasattr(use, 'user_telegram_id'):
                user_display = f'ID{use.user_telegram_id}'
            else:
                user_display = f'ID{use.user_id}'

            text += f'- {use_date} | {user_display}\n'
    else:
        text += texts.t('ADMIN_PROMO_STATS_NO_USES', '- Пока не было использований\n')

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BACK', '⬅️ Назад'),
                    callback_data=f'promo_manage_{promo.id}',
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def show_general_promocode_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    total_codes = await get_promocodes_count(db)
    active_codes = await get_promocodes_count(db, is_active=True)

    text = texts.t(
        'ADMIN_PROMO_GENERAL_STATS',
        '\n📊 <b>Общая статистика промокодов</b>\n\n'
        '📈 <b>Основные показатели:</b>\n'
        '- Всего промокодов: {total}\n'
        '- Активных: {active}\n'
        '- Неактивных: {inactive}\n\n'
        'Для детальной статистики выберите конкретный промокод из списка.\n',
    ).format(total=total_codes, active=active_codes, inactive=total_codes - active_codes)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BTN_TO_PROMOS', '🎫 К промокодам'),
                    callback_data='admin_promo_list',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PROMO_BACK', '⬅️ Назад'),
                    callback_data='admin_promocodes',
                )
            ],
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_promocodes_menu, F.data == 'admin_promocodes')
    dp.callback_query.register(show_promocodes_list, F.data == 'admin_promo_list')
    dp.callback_query.register(show_promocodes_list_page, F.data.startswith('admin_promo_list_page_'))
    dp.callback_query.register(start_promocode_creation, F.data == 'admin_promo_create')
    dp.callback_query.register(select_promocode_type, F.data.startswith('promo_type_'))
    dp.callback_query.register(process_promo_group_selection, F.data.startswith('promo_select_group_'))

    dp.callback_query.register(show_promocode_management, F.data.startswith('promo_manage_'))
    dp.callback_query.register(toggle_promocode_first_purchase, F.data.startswith('promo_toggle_first_'))
    dp.callback_query.register(toggle_promocode_status, F.data.startswith('promo_toggle_'))
    dp.callback_query.register(show_promocode_stats, F.data.startswith('promo_stats_'))

    dp.callback_query.register(start_edit_promocode_date, F.data.startswith('promo_edit_date_'))
    dp.callback_query.register(start_edit_promocode_amount, F.data.startswith('promo_edit_amount_'))
    dp.callback_query.register(start_edit_promocode_days, F.data.startswith('promo_edit_days_'))
    dp.callback_query.register(start_edit_promocode_uses, F.data.startswith('promo_edit_uses_'))
    dp.callback_query.register(show_general_promocode_stats, F.data == 'admin_promo_general_stats')

    dp.callback_query.register(show_promocode_edit_menu, F.data.regexp(r'^promo_edit_\d+$'))

    dp.callback_query.register(delete_promocode_confirmed, F.data.startswith('promo_delete_confirm_'))
    dp.callback_query.register(confirm_delete_promocode, F.data.startswith('promo_delete_'))

    dp.message.register(process_promocode_code, AdminStates.creating_promocode)
    dp.message.register(process_promocode_value, AdminStates.setting_promocode_value)
    dp.message.register(process_promocode_combo_days, AdminStates.setting_promocode_combo_days)
    dp.message.register(process_promocode_uses, AdminStates.setting_promocode_uses)
    dp.message.register(process_promocode_expiry, AdminStates.setting_promocode_expiry)
    dp.message.register(process_discount_hours, AdminStates.setting_discount_hours)
