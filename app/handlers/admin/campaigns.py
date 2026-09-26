import html
import re

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.campaign import (
    create_campaign,
    delete_campaign,
    get_campaign_by_id,
    get_campaign_by_start_parameter,
    get_campaign_statistics,
    get_campaigns_count,
    get_campaigns_list,
    get_campaigns_overview,
    update_campaign,
)
from app.database.crud.server_squad import get_all_server_squads, get_server_squad_by_id
from app.database.crud.tariff import get_all_tariffs, get_tariff_by_id
from app.database.models import User
from app.keyboards.admin import (
    get_admin_campaigns_keyboard,
    get_admin_pagination_keyboard,
    get_campaign_bonus_type_keyboard,
    get_campaign_edit_keyboard,
    get_campaign_management_keyboard,
    get_confirmation_keyboard,
)
from app.localization.texts import Texts, get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

_CAMPAIGN_PARAM_REGEX = re.compile(r'^[A-Za-z0-9_-]{3,32}$')
_CAMPAIGNS_PAGE_SIZE = 5


def _format_campaign_summary(campaign, texts) -> str:
    status = (
        texts.t('CAMPAIGN_STATUS_ACTIVE', '🟢 Активна')
        if campaign.is_active
        else texts.t('CAMPAIGN_STATUS_DISABLED', '⚪️ Выключена')
    )

    if campaign.is_balance_bonus:
        bonus_text = texts.format_price(campaign.balance_bonus_kopeks)
        bonus_info = texts.t('CAMPAIGN_SUMMARY_BALANCE_BONUS', '💰 Бонус на баланс: <b>{bonus}</b>').format(
            bonus=bonus_text
        )
    elif campaign.is_subscription_bonus:
        traffic_text = texts.format_traffic(campaign.subscription_traffic_gb or 0)
        device_limit = campaign.subscription_device_limit
        if device_limit is None:
            device_limit = settings.DEFAULT_DEVICE_LIMIT
        bonus_info = texts.t(
            'CAMPAIGN_SUMMARY_SUBSCRIPTION_BONUS',
            '📱 Пробная подписка: <b>{days} д.</b>\n'
            '🌐 Трафик: <b>{traffic}</b>\n'
            '📱 Устройства: <b>{devices}</b>',
        ).format(
            days=campaign.subscription_duration_days or 0,
            traffic=traffic_text,
            devices=Texts.format_device_limit(device_limit),
        )
    elif campaign.is_tariff_bonus:
        tariff_name = texts.t('CAMPAIGN_TARIFF_NOT_SELECTED', 'Не выбран')
        if hasattr(campaign, 'tariff') and campaign.tariff:
            tariff_name = campaign.tariff.name
        bonus_info = texts.t(
            'CAMPAIGN_SUMMARY_TARIFF_BONUS',
            '🎁 Тариф: <b>{name}</b>\n📅 Длительность: <b>{days} д.</b>',
        ).format(name=tariff_name, days=campaign.tariff_duration_days or 0)
    elif campaign.is_none_bonus:
        bonus_info = texts.t('CAMPAIGN_SUMMARY_NONE_BONUS', '🔗 Только ссылка (без награды)')
    else:
        bonus_info = texts.t('CAMPAIGN_SUMMARY_UNKNOWN_BONUS', '❓ Неизвестный тип бонуса')

    return texts.t(
        'CAMPAIGN_SUMMARY_BODY',
        '<b>{name}</b>\n'
        'Стартовый параметр: <code>{start_parameter}</code>\n'
        'Статус: {status}\n'
        '{bonus_info}\n',
    ).format(
        name=html.escape(campaign.name),
        start_parameter=html.escape(campaign.start_parameter),
        status=status,
        bonus_info=bonus_info,
    )


async def _get_bot_deep_link(callback: types.CallbackQuery, start_parameter: str) -> str:
    bot = await callback.bot.get_me()
    return f'https://t.me/{bot.username}?start={start_parameter}'


async def _get_bot_deep_link_from_message(message: types.Message, start_parameter: str) -> str:
    bot = await message.bot.get_me()
    return f'https://t.me/{bot.username}?start={start_parameter}'


def _build_campaign_servers_keyboard(
    servers,
    selected_uuids: list[str],
    *,
    toggle_prefix: str = 'campaign_toggle_server_',
    save_callback: str = 'campaign_servers_save',
    back_callback: str = 'admin_campaigns',
) -> types.InlineKeyboardMarkup:
    keyboard: list[list[types.InlineKeyboardButton]] = []

    for server in servers[:20]:
        is_selected = server.squad_uuid in selected_uuids
        emoji = '✅' if is_selected else ('⚪' if server.is_available else '🔒')
        text = f'{emoji} {server.display_name}'
        keyboard.append([types.InlineKeyboardButton(text=text, callback_data=f'{toggle_prefix}{server.id}')])

    keyboard.append(
        [
            types.InlineKeyboardButton(text='✅ Сохранить', callback_data=save_callback),
            types.InlineKeyboardButton(text='⬅️ Назад', callback_data=back_callback),
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _render_campaign_edit_menu(
    bot: Bot,
    chat_id: int,
    message_id: int,
    campaign,
    language: str,
    *,
    use_caption: bool = False,
):
    texts = get_texts(language)
    text = texts.t(
        'CAMPAIGN_EDIT_MENU',
        '✏️ <b>Редактирование кампании</b>\n\n{summary}\nВыберите, что изменить:',
    ).format(summary=_format_campaign_summary(campaign, texts))

    edit_kwargs = dict(
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=get_campaign_edit_keyboard(
            campaign.id,
            bonus_type=campaign.bonus_type,
            language=language,
        ),
        parse_mode='HTML',
    )

    if use_caption:
        await bot.edit_message_caption(
            caption=text,
            **edit_kwargs,
        )
    else:
        await bot.edit_message_text(
            text=text,
            **edit_kwargs,
        )


@admin_required
@error_handler
async def show_campaigns_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    overview = await get_campaigns_overview(db)

    text = texts.t(
        'CAMPAIGN_MENU',
        '📣 <b>Рекламные кампании</b>\n\n'
        'Всего кампаний: <b>{total}</b>\n'
        'Активных: <b>{active}</b> | Выключены: <b>{inactive}</b>\n'
        'Регистраций: <b>{registrations}</b>\n'
        'Выдано баланса: <b>{balance}</b>\n'
        'Выдано подписок: <b>{subscriptions}</b>',
    ).format(
        total=overview["total"],
        active=overview["active"],
        inactive=overview["inactive"],
        registrations=overview["registrations"],
        balance=texts.format_price(overview["balance_total"]),
        subscriptions=overview["subscription_total"],
    )

    await callback.message.edit_text(
        text,
        reply_markup=get_admin_campaigns_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_campaigns_overall_stats(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    overview = await get_campaigns_overview(db)

    text = [texts.t('CAMPAIGN_STATS_OVERALL_TITLE', '📊 <b>Общая статистика кампаний</b>\n')]
    text.append(texts.t('CAMPAIGN_STATS_OVERALL_TOTAL', 'Всего кампаний: <b>{total}</b>').format(
        total=overview["total"]
    ))
    text.append(texts.t(
        'CAMPAIGN_STATS_OVERALL_ACTIVE',
        'Активны: <b>{active}</b>, выключены: <b>{inactive}</b>',
    ).format(active=overview["active"], inactive=overview["inactive"]))
    text.append(texts.t('CAMPAIGN_STATS_OVERALL_REGISTRATIONS', 'Всего регистраций: <b>{registrations}</b>').format(
        registrations=overview["registrations"]
    ))
    text.append(texts.t('CAMPAIGN_STATS_OVERALL_BALANCE', 'Суммарно выдано баланса: <b>{balance}</b>').format(
        balance=texts.format_price(overview["balance_total"])
    ))
    text.append(texts.t('CAMPAIGN_STATS_OVERALL_SUBSCRIPTIONS', 'Выдано подписок: <b>{subscriptions}</b>').format(
        subscriptions=overview["subscription_total"]
    ))

    await callback.message.edit_text(
        '\n'.join(text),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(
                text=texts.t('CAMPAIGN_BTN_BACK', '⬅️ Назад'), callback_data='admin_campaigns'
            )]]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_campaigns_list(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    page = 1
    if callback.data.startswith('admin_campaigns_list_page_'):
        try:
            page = int(callback.data.split('_')[-1])
        except ValueError:
            page = 1

    offset = (page - 1) * _CAMPAIGNS_PAGE_SIZE
    campaigns = await get_campaigns_list(
        db,
        offset=offset,
        limit=_CAMPAIGNS_PAGE_SIZE,
    )
    total = await get_campaigns_count(db)
    total_pages = max(1, (total + _CAMPAIGNS_PAGE_SIZE - 1) // _CAMPAIGNS_PAGE_SIZE)

    if not campaigns:
        await callback.message.edit_text(
            texts.t('CAMPAIGN_LIST_EMPTY', '❌ Рекламные кампании не найдены.'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(
                        text=texts.t('CAMPAIGN_BTN_CREATE', '➕ Создать'), callback_data='admin_campaigns_create'
                    )],
                    [types.InlineKeyboardButton(
                        text=texts.t('CAMPAIGN_BTN_BACK', '⬅️ Назад'), callback_data='admin_campaigns'
                    )],
                ]
            ),
        )
        await callback.answer()
        return

    text_lines = [texts.t('CAMPAIGN_LIST_TITLE', '📋 <b>Список кампаний</b>\n')]

    for campaign in campaigns:
        # Access from instance dict to avoid MissingGreenlet on lazy load
        regs = sa_inspect(campaign).dict.get('registrations', []) or []
        registrations = len(regs)
        total_balance = sum(r.balance_bonus_kopeks or 0 for r in regs)
        status = '🟢' if campaign.is_active else '⚪'
        line = texts.t(
            'CAMPAIGN_LIST_ITEM',
            '{status} <b>{name}</b> — <code>{start_parameter}</code>\n'
            '   Регистраций: {registrations}, баланс: {balance}',
        ).format(
            status=status,
            name=html.escape(campaign.name),
            start_parameter=html.escape(campaign.start_parameter),
            registrations=registrations,
            balance=texts.format_price(total_balance),
        )
        if campaign.is_subscription_bonus:
            line += texts.t('CAMPAIGN_LIST_ITEM_SUBSCRIPTION', ', подписка: {days} д.').format(
                days=campaign.subscription_duration_days or 0
            )
        else:
            line += texts.t('CAMPAIGN_LIST_ITEM_BALANCE', ', бонус: баланс')
        text_lines.append(line)

    keyboard_rows = [
        [
            types.InlineKeyboardButton(
                text=f'🔍 {campaign.name}',
                callback_data=f'admin_campaign_manage_{campaign.id}',
            )
        ]
        for campaign in campaigns
    ]

    pagination = get_admin_pagination_keyboard(
        current_page=page,
        total_pages=total_pages,
        callback_prefix='admin_campaigns_list',
        back_callback='admin_campaigns',
        language=db_user.language,
    )

    keyboard_rows.extend(pagination.inline_keyboard)

    await callback.message.edit_text(
        '\n'.join(text_lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_campaign_detail(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)

    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    stats = await get_campaign_statistics(db, campaign_id)
    deep_link = await _get_bot_deep_link(callback, campaign.start_parameter)

    text = [texts.t('CAMPAIGN_DETAIL_TITLE', '📣 <b>Управление кампанией</b>\n')]
    text.append(_format_campaign_summary(campaign, texts))
    text.append(texts.t('CAMPAIGN_DETAIL_LINK', '🔗 Ссылка: <code>{link}</code>').format(link=deep_link))
    text.append(texts.t('CAMPAIGN_DETAIL_STATS_HEADER', '\n📊 <b>Статистика</b>'))
    text.append(texts.t('CAMPAIGN_DETAIL_REGISTRATIONS', '• Регистраций: <b>{registrations}</b>').format(
        registrations=stats["registrations"]
    ))
    text.append(texts.t('CAMPAIGN_DETAIL_BALANCE_ISSUED', '• Выдано баланса: <b>{balance}</b>').format(
        balance=texts.format_price(stats["balance_issued"])
    ))
    text.append(texts.t('CAMPAIGN_DETAIL_SUBSCRIPTION_ISSUED', '• Выдано подписок: <b>{subscriptions}</b>').format(
        subscriptions=stats["subscription_issued"]
    ))
    text.append(texts.t('CAMPAIGN_DETAIL_REVENUE', '• Доход: <b>{revenue}</b>').format(
        revenue=texts.format_price(stats["total_revenue_kopeks"])
    ))
    text.append(texts.t(
        'CAMPAIGN_DETAIL_TRIAL',
        '• Получили триал: <b>{trial}</b> (активно: {active})',
    ).format(trial=stats["trial_users_count"], active=stats["active_trials_count"]))
    text.append(texts.t(
        'CAMPAIGN_DETAIL_CONVERSIONS',
        '• Конверсий в оплату: <b>{count}</b> / пользователей с оплатой: {paid}',
    ).format(count=stats["conversion_count"], paid=stats["paid_users_count"]))
    text.append(texts.t('CAMPAIGN_DETAIL_CONVERSION_RATE', '• Конверсия в оплату: <b>{rate:.1f}%</b>').format(
        rate=stats["conversion_rate"]
    ))
    text.append(texts.t('CAMPAIGN_DETAIL_TRIAL_CONVERSION_RATE', '• Конверсия триала: <b>{rate:.1f}%</b>').format(
        rate=stats["trial_conversion_rate"]
    ))
    text.append(texts.t(
        'CAMPAIGN_DETAIL_AVG_REVENUE',
        '• Средний доход на пользователя: <b>{amount}</b>',
    ).format(amount=texts.format_price(stats["avg_revenue_per_user_kopeks"])))
    text.append(texts.t('CAMPAIGN_DETAIL_AVG_FIRST_PAYMENT', '• Средний первый платеж: <b>{amount}</b>').format(
        amount=texts.format_price(stats["avg_first_payment_kopeks"])
    ))
    if stats['last_registration']:
        text.append(texts.t('CAMPAIGN_DETAIL_LAST_REGISTRATION', '• Последняя: {date}').format(
            date=stats["last_registration"].strftime("%d.%m.%Y %H:%M")
        ))

    await callback.message.edit_text(
        '\n'.join(text),
        reply_markup=get_campaign_management_keyboard(campaign.id, campaign.is_active, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_campaign_edit_menu(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)

    if not campaign:
        await state.clear()
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    await state.clear()

    use_caption = bool(callback.message.caption) and not bool(callback.message.text)

    await _render_campaign_edit_menu(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        campaign,
        db_user.language,
        use_caption=use_caption,
    )
    await callback.answer()


@admin_required
@error_handler
async def start_edit_campaign_name(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_name)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_edit_message_is_caption=is_caption,
    )

    await callback.message.edit_text(
        texts.t(
            'CAMPAIGN_EDIT_NAME_PROMPT',
            '✏️ <b>Изменение названия кампании</b>\n\n'
            'Текущее название: <b>{name}</b>\n'
            'Введите новое название (3-100 символов):',
        ).format(name=html.escape(campaign.name)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CAMPAIGN_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_name(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer(
            texts.t('CAMPAIGN_EDIT_SESSION_EXPIRED', '❌ Сессия редактирования устарела. Попробуйте снова.')
        )
        await state.clear()
        return

    new_name = message.text.strip()
    if len(new_name) < 3 or len(new_name) > 100:
        await message.answer(
            texts.t(
                'CAMPAIGN_NAME_INVALID_LENGTH',
                '❌ Название должно содержать от 3 до 100 символов. Попробуйте снова.',
            )
        )
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'))
        await state.clear()
        return

    await update_campaign(db, campaign, name=new_name)
    await state.clear()

    await message.answer(texts.t('CAMPAIGN_NAME_UPDATED', '✅ Название обновлено.'))

    edit_message_id = data.get('campaign_edit_message_id')
    edit_message_is_caption = data.get('campaign_edit_message_is_caption', False)
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
            use_caption=edit_message_is_caption,
        )


@admin_required
@error_handler
async def start_edit_campaign_start_parameter(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_start)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_edit_message_is_caption=is_caption,
    )

    await callback.message.edit_text(
        texts.t(
            'CAMPAIGN_EDIT_START_PROMPT',
            '🔗 <b>Изменение стартового параметра</b>\n\n'
            'Текущий параметр: <code>{param}</code>\n'
            'Введите новый параметр (латинские буквы, цифры, - или _, 3-32 символа):',
        ).format(param=campaign.start_parameter),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CAMPAIGN_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_start_parameter(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer(
            texts.t('CAMPAIGN_EDIT_SESSION_EXPIRED', '❌ Сессия редактирования устарела. Попробуйте снова.')
        )
        await state.clear()
        return

    new_param = message.text.strip()
    if not _CAMPAIGN_PARAM_REGEX.match(new_param):
        await message.answer(
            texts.t(
                'CAMPAIGN_PARAM_INVALID',
                '❌ Разрешены только латинские буквы, цифры, символы - и _. Длина 3-32 символа.',
            )
        )
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'))
        await state.clear()
        return

    existing = await get_campaign_by_start_parameter(db, new_param)
    if existing and existing.id != campaign_id:
        await message.answer(
            texts.t('CAMPAIGN_PARAM_IN_USE', '❌ Такой параметр уже используется. Введите другой вариант.')
        )
        return

    await update_campaign(db, campaign, start_parameter=new_param)
    await state.clear()

    await message.answer(texts.t('CAMPAIGN_START_UPDATED', '✅ Стартовый параметр обновлен.'))

    edit_message_id = data.get('campaign_edit_message_id')
    edit_message_is_caption = data.get('campaign_edit_message_is_caption', False)
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
            use_caption=edit_message_is_caption,
        )


@admin_required
@error_handler
async def start_edit_campaign_balance_bonus(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    if not campaign.is_balance_bonus:
        await callback.answer(texts.t('CAMPAIGN_WRONG_BONUS_TYPE', '❌ У кампании другой тип бонуса'), show_alert=True)
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_balance)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_edit_message_is_caption=is_caption,
    )

    await callback.message.edit_text(
        texts.t(
            'CAMPAIGN_EDIT_BALANCE_PROMPT',
            '💰 <b>Изменение бонуса на баланс</b>\n\n'
            'Текущий бонус: <b>{bonus}</b>\n'
            'Введите новую сумму в рублях (например, 100 или 99.5):',
        ).format(bonus=texts.format_price(campaign.balance_bonus_kopeks)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CAMPAIGN_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_balance_bonus(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer(
            texts.t('CAMPAIGN_EDIT_SESSION_EXPIRED', '❌ Сессия редактирования устарела. Попробуйте снова.')
        )
        await state.clear()
        return

    try:
        amount_rubles = float(message.text.replace(',', '.'))
    except ValueError:
        await message.answer(texts.t('CAMPAIGN_AMOUNT_INVALID', '❌ Введите корректную сумму (например, 100 или 99.5)'))
        return

    if amount_rubles <= 0:
        await message.answer(texts.t('CAMPAIGN_AMOUNT_NOT_POSITIVE', '❌ Сумма должна быть больше нуля'))
        return

    amount_kopeks = int(round(amount_rubles * 100))

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'))
        await state.clear()
        return

    if not campaign.is_balance_bonus:
        await message.answer(texts.t('CAMPAIGN_WRONG_BONUS_TYPE', '❌ У кампании другой тип бонуса'))
        await state.clear()
        return

    await update_campaign(db, campaign, balance_bonus_kopeks=amount_kopeks)
    await state.clear()

    await message.answer(texts.t('CAMPAIGN_BALANCE_UPDATED', '✅ Бонус обновлен.'))

    edit_message_id = data.get('campaign_edit_message_id')
    edit_message_is_caption = data.get('campaign_edit_message_is_caption', False)
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
            use_caption=edit_message_is_caption,
        )


async def _ensure_subscription_campaign(message_or_callback, campaign) -> bool:
    if campaign.is_balance_bonus:
        if isinstance(message_or_callback, types.CallbackQuery):
            await message_or_callback.answer(
                '❌ Для этой кампании доступен только бонус на баланс',
                show_alert=True,
            )
        else:
            await message_or_callback.answer('❌ Для этой кампании нельзя изменить параметры подписки')
        return False
    return True


@admin_required
@error_handler
async def start_edit_campaign_subscription_days(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign):
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_subscription_days)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_edit_message_is_caption=is_caption,
    )

    await callback.message.edit_text(
        texts.t(
            'CAMPAIGN_EDIT_SUB_DAYS_PROMPT',
            '📅 <b>Изменение длительности подписки</b>\n\n'
            'Текущее значение: <b>{days} д.</b>\n'
            'Введите новое количество дней (1-730):',
        ).format(days=campaign.subscription_duration_days or 0),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CAMPAIGN_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_subscription_days(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer(
            texts.t('CAMPAIGN_EDIT_SESSION_EXPIRED', '❌ Сессия редактирования устарела. Попробуйте снова.')
        )
        await state.clear()
        return

    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer(texts.t('CAMPAIGN_DAYS_INVALID_INT', '❌ Введите число дней (1-730)'))
        return

    if days <= 0 or days > 730:
        await message.answer(texts.t('CAMPAIGN_DAYS_OUT_OF_RANGE', '❌ Длительность должна быть от 1 до 730 дней'))
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'))
        await state.clear()
        return

    if not await _ensure_subscription_campaign(message, campaign):
        await state.clear()
        return

    await update_campaign(db, campaign, subscription_duration_days=days)
    await state.clear()

    await message.answer(texts.t('CAMPAIGN_SUB_DAYS_UPDATED', '✅ Длительность подписки обновлена.'))

    edit_message_id = data.get('campaign_edit_message_id')
    edit_message_is_caption = data.get('campaign_edit_message_is_caption', False)
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
            use_caption=edit_message_is_caption,
        )


@admin_required
@error_handler
async def start_edit_campaign_subscription_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign):
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_subscription_traffic)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_edit_message_is_caption=is_caption,
    )

    current_traffic = campaign.subscription_traffic_gb or 0
    traffic_text = (
        texts.t('CAMPAIGN_TRAFFIC_UNLIMITED', 'безлимит')
        if current_traffic == 0
        else texts.t('CAMPAIGN_TRAFFIC_GB', '{value} ГБ').format(value=current_traffic)
    )

    await callback.message.edit_text(
        texts.t(
            'CAMPAIGN_EDIT_TRAFFIC_PROMPT',
            '🌐 <b>Изменение лимита трафика</b>\n\n'
            'Текущее значение: <b>{traffic}</b>\n'
            'Введите новый лимит в ГБ (0 = безлимит, максимум 10000):',
        ).format(traffic=traffic_text),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CAMPAIGN_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_subscription_traffic(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer(
            texts.t('CAMPAIGN_EDIT_SESSION_EXPIRED', '❌ Сессия редактирования устарела. Попробуйте снова.')
        )
        await state.clear()
        return

    try:
        traffic = int(message.text.strip())
    except ValueError:
        await message.answer(texts.t('CAMPAIGN_TRAFFIC_INVALID_INT', '❌ Введите целое число (0 или больше)'))
        return

    if traffic < 0 or traffic > 10000:
        await message.answer(
            texts.t('CAMPAIGN_TRAFFIC_OUT_OF_RANGE', '❌ Лимит трафика должен быть от 0 до 10000 ГБ')
        )
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'))
        await state.clear()
        return

    if not await _ensure_subscription_campaign(message, campaign):
        await state.clear()
        return

    await update_campaign(db, campaign, subscription_traffic_gb=traffic)
    await state.clear()

    await message.answer(texts.t('CAMPAIGN_TRAFFIC_UPDATED', '✅ Лимит трафика обновлен.'))

    edit_message_id = data.get('campaign_edit_message_id')
    edit_message_is_caption = data.get('campaign_edit_message_is_caption', False)
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
            use_caption=edit_message_is_caption,
        )


@admin_required
@error_handler
async def start_edit_campaign_subscription_devices(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign):
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_subscription_devices)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_edit_message_is_caption=is_caption,
    )

    current_devices = campaign.subscription_device_limit
    if current_devices is None:
        current_devices = settings.DEFAULT_DEVICE_LIMIT

    await callback.message.edit_text(
        texts.t(
            'CAMPAIGN_EDIT_DEVICES_PROMPT',
            '📱 <b>Изменение лимита устройств</b>\n\n'
            'Текущее значение: <b>{current}</b>\n'
            'Введите новое количество (1-{max}):',
        ).format(current=current_devices, max=settings.MAX_DEVICES_LIMIT),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CAMPAIGN_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_subscription_devices(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer(
            texts.t('CAMPAIGN_EDIT_SESSION_EXPIRED', '❌ Сессия редактирования устарела. Попробуйте снова.')
        )
        await state.clear()
        return

    try:
        devices = int(message.text.strip())
    except ValueError:
        await message.answer(texts.t('CAMPAIGN_DEVICES_INVALID_INT', '❌ Введите целое число устройств'))
        return

    if devices < 1 or devices > settings.MAX_DEVICES_LIMIT:
        await message.answer(
            texts.t('CAMPAIGN_DEVICES_OUT_OF_RANGE', '❌ Количество устройств должно быть от 1 до {max}').format(
                max=settings.MAX_DEVICES_LIMIT
            )
        )
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'))
        await state.clear()
        return

    if not await _ensure_subscription_campaign(message, campaign):
        await state.clear()
        return

    await update_campaign(db, campaign, subscription_device_limit=devices)
    await state.clear()

    await message.answer(texts.t('CAMPAIGN_DEVICES_UPDATED', '✅ Лимит устройств обновлен.'))

    edit_message_id = data.get('campaign_edit_message_id')
    edit_message_is_caption = data.get('campaign_edit_message_is_caption', False)
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
            use_caption=edit_message_is_caption,
        )


@admin_required
@error_handler
async def start_edit_campaign_subscription_servers(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign):
        return

    servers, _ = await get_all_server_squads(db, available_only=False)
    if not servers:
        await callback.answer(
            texts.t('CAMPAIGN_NO_SERVERS_EDIT', '❌ Не найдены доступные серверы. Добавьте серверы перед изменением.'),
            show_alert=True,
        )
        return

    selected = list(campaign.subscription_squads or [])

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_subscription_servers)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_subscription_squads=selected,
        campaign_edit_message_is_caption=is_caption,
    )

    keyboard = _build_campaign_servers_keyboard(
        servers,
        selected,
        toggle_prefix=f'campaign_edit_toggle_{campaign_id}_',
        save_callback=f'campaign_edit_servers_save_{campaign_id}',
        back_callback=f'admin_campaign_edit_{campaign_id}',
    )

    await callback.message.edit_text(
        texts.t(
            'CAMPAIGN_EDIT_SERVERS_PROMPT',
            '🌍 <b>Редактирование доступных серверов</b>\n\n'
            'Нажмите на сервер, чтобы добавить или убрать его из кампании.\n'
            'После выбора нажмите "✅ Сохранить".',
        ),
        reply_markup=keyboard,
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_edit_campaign_server(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split('_')
    try:
        server_id = int(parts[-1])
    except (ValueError, IndexError):
        await callback.answer(
            texts.t('CAMPAIGN_SERVER_UNDETERMINED', '❌ Не удалось определить сервер'), show_alert=True
        )
        return

    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await callback.answer(
            texts.t('CAMPAIGN_EDIT_SESSION_EXPIRED_ALERT', '❌ Сессия редактирования устарела'), show_alert=True
        )
        await state.clear()
        return

    server = await get_server_squad_by_id(db, server_id)
    if not server:
        await callback.answer(texts.t('CAMPAIGN_SERVER_NOT_FOUND', '❌ Сервер не найден'), show_alert=True)
        return

    selected = list(data.get('campaign_subscription_squads', []))

    if server.squad_uuid in selected:
        selected.remove(server.squad_uuid)
    else:
        selected.append(server.squad_uuid)

    await state.update_data(campaign_subscription_squads=selected)

    servers, _ = await get_all_server_squads(db, available_only=False)
    keyboard = _build_campaign_servers_keyboard(
        servers,
        selected,
        toggle_prefix=f'campaign_edit_toggle_{campaign_id}_',
        save_callback=f'campaign_edit_servers_save_{campaign_id}',
        back_callback=f'admin_campaign_edit_{campaign_id}',
    )

    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def save_edit_campaign_subscription_servers(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await callback.answer(
            texts.t('CAMPAIGN_EDIT_SESSION_EXPIRED_ALERT', '❌ Сессия редактирования устарела'), show_alert=True
        )
        await state.clear()
        return

    selected = list(data.get('campaign_subscription_squads', []))
    if not selected:
        await callback.answer(
            texts.t('CAMPAIGN_SELECT_AT_LEAST_ONE_SERVER', '❗ Выберите хотя бы один сервер'), show_alert=True
        )
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await state.clear()
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign):
        await state.clear()
        return

    await update_campaign(db, campaign, subscription_squads=selected)
    await state.clear()

    use_caption = bool(callback.message.caption) and not bool(callback.message.text)

    await _render_campaign_edit_menu(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        campaign,
        db_user.language,
        use_caption=use_caption,
    )
    await callback.answer(texts.t('CAMPAIGN_SAVED', '✅ Сохранено'))


@admin_required
@error_handler
async def toggle_campaign_status(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    new_status = not campaign.is_active
    await update_campaign(db, campaign, is_active=new_status)
    status_text = 'включена' if new_status else 'выключена'
    logger.info('🔄 Кампания переключена', campaign_id=campaign_id, status_text=status_text)

    await show_campaign_detail(callback, db_user, db)


@admin_required
@error_handler
async def show_campaign_stats(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    stats = await get_campaign_statistics(db, campaign_id)

    text = [texts.t('CAMPAIGN_STATS_TITLE', '📊 <b>Статистика кампании</b>\n')]
    text.append(_format_campaign_summary(campaign, texts))
    text.append(texts.t('CAMPAIGN_STATS_REGISTRATIONS', 'Регистраций: <b>{registrations}</b>').format(
        registrations=stats["registrations"]
    ))
    text.append(texts.t('CAMPAIGN_STATS_BALANCE_ISSUED', 'Выдано баланса: <b>{balance}</b>').format(
        balance=texts.format_price(stats["balance_issued"])
    ))
    text.append(texts.t('CAMPAIGN_STATS_SUBSCRIPTION_ISSUED', 'Выдано подписок: <b>{subscriptions}</b>').format(
        subscriptions=stats["subscription_issued"]
    ))
    if stats['last_registration']:
        text.append(texts.t('CAMPAIGN_STATS_LAST_REGISTRATION', 'Последняя регистрация: {date}').format(
            date=stats["last_registration"].strftime("%d.%m.%Y %H:%M")
        ))

    await callback.message.edit_text(
        '\n'.join(text),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CAMPAIGN_BTN_BACK', '⬅️ Назад'),
                        callback_data=f'admin_campaign_manage_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_delete_campaign(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    text = texts.t(
        'CAMPAIGN_DELETE_CONFIRM',
        '🗑️ <b>Удаление кампании</b>\n\n'
        'Название: <b>{name}</b>\n'
        'Параметр: <code>{param}</code>\n\n'
        'Вы уверены, что хотите удалить кампанию?',
    ).format(name=html.escape(campaign.name), param=html.escape(campaign.start_parameter))

    await callback.message.edit_text(
        text,
        reply_markup=get_confirmation_keyboard(
            confirm_action=f'admin_campaign_delete_confirm_{campaign_id}',
            cancel_action=f'admin_campaign_manage_{campaign_id}',
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_campaign_confirmed(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    await delete_campaign(db, campaign)
    await callback.message.edit_text(
        texts.t('CAMPAIGN_DELETED', '✅ Кампания удалена.'),
        reply_markup=get_admin_campaigns_keyboard(db_user.language),
    )
    await callback.answer(texts.t('CAMPAIGN_DELETED_TOAST', 'Удалено'))


@admin_required
@error_handler
async def start_campaign_creation(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    await state.clear()
    await callback.message.edit_text(
        texts.t('CAMPAIGN_CREATE_NAME_PROMPT', '🆕 <b>Создание рекламной кампании</b>\n\nВведите название кампании:'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(
                text=texts.t('CAMPAIGN_BTN_BACK', '⬅️ Назад'), callback_data='admin_campaigns'
            )]]
        ),
    )
    await state.set_state(AdminStates.creating_campaign_name)
    await callback.answer()


@admin_required
@error_handler
async def process_campaign_name(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    name = message.text.strip()
    if len(name) < 3 or len(name) > 100:
        await message.answer(
            texts.t(
                'CAMPAIGN_NAME_INVALID_LENGTH',
                '❌ Название должно содержать от 3 до 100 символов. Попробуйте снова.',
            )
        )
        return

    await state.update_data(campaign_name=name)
    await state.set_state(AdminStates.creating_campaign_start)
    await message.answer(
        texts.t('CAMPAIGN_CREATE_START_PROMPT', '🔗 Теперь введите параметр старта (латинские буквы, цифры, - или _):'),
    )


@admin_required
@error_handler
async def process_campaign_start_parameter(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    start_param = message.text.strip()
    if not _CAMPAIGN_PARAM_REGEX.match(start_param):
        await message.answer(
            texts.t(
                'CAMPAIGN_PARAM_INVALID',
                '❌ Разрешены только латинские буквы, цифры, символы - и _. Длина 3-32 символа.',
            )
        )
        return

    existing = await get_campaign_by_start_parameter(db, start_param)
    if existing:
        await message.answer(
            texts.t('CAMPAIGN_PARAM_EXISTS', '❌ Кампания с таким параметром уже существует. Введите другой параметр.')
        )
        return

    await state.update_data(campaign_start_parameter=start_param)
    await state.set_state(AdminStates.creating_campaign_bonus)
    await message.answer(
        texts.t('CAMPAIGN_SELECT_BONUS_TYPE', '🎯 Выберите тип бонуса для кампании:'),
        reply_markup=get_campaign_bonus_type_keyboard(db_user.language),
    )


@admin_required
@error_handler
async def select_campaign_bonus_type(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    # Определяем тип бонуса из callback_data
    if callback.data.endswith('balance'):
        bonus_type = 'balance'
    elif callback.data.endswith('subscription'):
        bonus_type = 'subscription'
    elif callback.data.endswith('tariff'):
        bonus_type = 'tariff'
    elif callback.data.endswith('none'):
        bonus_type = 'none'
    else:
        bonus_type = 'balance'

    await state.update_data(campaign_bonus_type=bonus_type)

    texts = get_texts(db_user.language)

    if bonus_type == 'balance':
        await state.set_state(AdminStates.creating_campaign_balance)
        await callback.message.edit_text(
            texts.t('CAMPAIGN_CREATE_BALANCE_PROMPT', '💰 Введите сумму бонуса на баланс (в рублях):'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(
                    text=texts.t('CAMPAIGN_BTN_BACK', '⬅️ Назад'), callback_data='admin_campaigns'
                )]]
            ),
        )
    elif bonus_type == 'subscription':
        await state.set_state(AdminStates.creating_campaign_subscription_days)
        await callback.message.edit_text(
            texts.t('CAMPAIGN_CREATE_SUB_DAYS_PROMPT', '📅 Введите длительность пробной подписки в днях (1-730):'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(
                    text=texts.t('CAMPAIGN_BTN_BACK', '⬅️ Назад'), callback_data='admin_campaigns'
                )]]
            ),
        )
    elif bonus_type == 'tariff':
        # Показываем выбор тарифа
        tariffs = await get_all_tariffs(db, include_inactive=False)
        if not tariffs:
            await callback.answer(
                texts.t('CAMPAIGN_NO_TARIFFS_CREATE', '❌ Нет доступных тарифов. Сначала создайте тариф.'),
                show_alert=True,
            )
            return

        keyboard = []
        for tariff in tariffs[:15]:  # Максимум 15 тарифов
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'🎁 {tariff.name}',
                        callback_data=f'campaign_select_tariff_{tariff.id}',
                    )
                ]
            )
        keyboard.append([types.InlineKeyboardButton(
            text=texts.t('CAMPAIGN_BTN_BACK', '⬅️ Назад'), callback_data='admin_campaigns'
        )])

        await state.set_state(AdminStates.creating_campaign_tariff_select)
        await callback.message.edit_text(
            texts.t('CAMPAIGN_SELECT_TARIFF_PROMPT', '🎁 Выберите тариф для выдачи:'),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
    elif bonus_type == 'none':
        # Сразу создаём кампанию без бонуса
        data = await state.get_data()
        campaign = await create_campaign(
            db,
            name=data['campaign_name'],
            start_parameter=data['campaign_start_parameter'],
            bonus_type='none',
            created_by=db_user.id,
        )
        await state.clear()

        deep_link = await _get_bot_deep_link(callback, campaign.start_parameter)
        summary = _format_campaign_summary(campaign, texts)
        text = texts.t(
            'CAMPAIGN_CREATED',
            '✅ <b>Кампания создана!</b>\n\n{summary}\n🔗 Ссылка: <code>{link}</code>',
        ).format(summary=summary, link=deep_link)

        await callback.message.edit_text(
            text,
            reply_markup=get_campaign_management_keyboard(campaign.id, campaign.is_active, db_user.language),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_campaign_balance_value(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    try:
        amount_rubles = float(message.text.replace(',', '.'))
    except ValueError:
        await message.answer(
            texts.t('CAMPAIGN_AMOUNT_INVALID', '❌ Введите корректную сумму (например, 100 или 99.5)')
        )
        return

    if amount_rubles <= 0:
        await message.answer(texts.t('CAMPAIGN_AMOUNT_NOT_POSITIVE', '❌ Сумма должна быть больше нуля'))
        return

    amount_kopeks = int(round(amount_rubles * 100))
    data = await state.get_data()

    campaign = await create_campaign(
        db,
        name=data['campaign_name'],
        start_parameter=data['campaign_start_parameter'],
        bonus_type='balance',
        balance_bonus_kopeks=amount_kopeks,
        created_by=db_user.id,
    )

    await state.clear()

    deep_link = await _get_bot_deep_link_from_message(message, campaign.start_parameter)
    summary = _format_campaign_summary(campaign, texts)
    text = texts.t(
        'CAMPAIGN_CREATED',
        '✅ <b>Кампания создана!</b>\n\n{summary}\n🔗 Ссылка: <code>{link}</code>',
    ).format(summary=summary, link=deep_link)

    await message.answer(
        text,
        reply_markup=get_campaign_management_keyboard(campaign.id, campaign.is_active, db_user.language),
    )


@admin_required
@error_handler
async def process_campaign_subscription_days(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer(texts.t('CAMPAIGN_DAYS_INVALID_INT', '❌ Введите число дней (1-730)'))
        return

    if days <= 0 or days > 730:
        await message.answer(
            texts.t('CAMPAIGN_DAYS_OUT_OF_RANGE', '❌ Длительность должна быть от 1 до 730 дней')
        )
        return

    await state.update_data(campaign_subscription_days=days)
    await state.set_state(AdminStates.creating_campaign_subscription_traffic)
    await message.answer(
        texts.t('CAMPAIGN_CREATE_TRAFFIC_PROMPT', '🌐 Введите лимит трафика в ГБ (0 = безлимит):')
    )


@admin_required
@error_handler
async def process_campaign_subscription_traffic(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    try:
        traffic = int(message.text.strip())
    except ValueError:
        await message.answer(texts.t('CAMPAIGN_TRAFFIC_INVALID_INT', '❌ Введите целое число (0 или больше)'))
        return

    if traffic < 0 or traffic > 10000:
        await message.answer(
            texts.t('CAMPAIGN_TRAFFIC_OUT_OF_RANGE', '❌ Лимит трафика должен быть от 0 до 10000 ГБ')
        )
        return

    await state.update_data(campaign_subscription_traffic=traffic)
    await state.set_state(AdminStates.creating_campaign_subscription_devices)
    await message.answer(
        texts.t('CAMPAIGN_CREATE_DEVICES_PROMPT', '📱 Введите количество устройств (1-{max}):').format(
            max=settings.MAX_DEVICES_LIMIT
        )
    )


@admin_required
@error_handler
async def process_campaign_subscription_devices(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    try:
        devices = int(message.text.strip())
    except ValueError:
        await message.answer(texts.t('CAMPAIGN_DEVICES_INVALID_INT', '❌ Введите целое число устройств'))
        return

    if devices < 1 or devices > settings.MAX_DEVICES_LIMIT:
        await message.answer(
            texts.t(
                'CAMPAIGN_DEVICES_OUT_OF_RANGE', '❌ Количество устройств должно быть от 1 до {max}'
            ).format(max=settings.MAX_DEVICES_LIMIT)
        )
        return

    await state.update_data(campaign_subscription_devices=devices)
    await state.update_data(campaign_subscription_squads=[])
    await state.set_state(AdminStates.creating_campaign_subscription_servers)

    servers, _ = await get_all_server_squads(db, available_only=False)
    if not servers:
        await message.answer(
            texts.t(
                'CAMPAIGN_NO_SERVERS_CREATE',
                '❌ Не найдены доступные серверы. Добавьте сервера перед созданием кампании.',
            ),
        )
        await state.clear()
        return

    keyboard = _build_campaign_servers_keyboard(servers, [])
    await message.answer(
        texts.t(
            'CAMPAIGN_CREATE_SERVERS_PROMPT',
            '🌍 Выберите серверы, которые будут доступны по подписке (максимум 20 отображаются).',
        ),
        reply_markup=keyboard,
    )


@admin_required
@error_handler
async def toggle_campaign_server(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)
    if not server:
        await callback.answer(texts.t('CAMPAIGN_SERVER_NOT_FOUND', '❌ Сервер не найден'), show_alert=True)
        return

    data = await state.get_data()
    selected = list(data.get('campaign_subscription_squads', []))

    if server.squad_uuid in selected:
        selected.remove(server.squad_uuid)
    else:
        selected.append(server.squad_uuid)

    await state.update_data(campaign_subscription_squads=selected)

    servers, _ = await get_all_server_squads(db, available_only=False)
    keyboard = _build_campaign_servers_keyboard(servers, selected)

    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def finalize_campaign_subscription(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    selected = data.get('campaign_subscription_squads', [])

    if not selected:
        await callback.answer(
            texts.t('CAMPAIGN_SELECT_AT_LEAST_ONE_SERVER', '❗ Выберите хотя бы один сервер'),
            show_alert=True,
        )
        return

    campaign = await create_campaign(
        db,
        name=data['campaign_name'],
        start_parameter=data['campaign_start_parameter'],
        bonus_type='subscription',
        subscription_duration_days=data.get('campaign_subscription_days'),
        subscription_traffic_gb=data.get('campaign_subscription_traffic'),
        subscription_device_limit=data.get('campaign_subscription_devices'),
        subscription_squads=selected,
        created_by=db_user.id,
    )

    await state.clear()

    deep_link = await _get_bot_deep_link(callback, campaign.start_parameter)
    summary = _format_campaign_summary(campaign, texts)
    text = texts.t(
        'CAMPAIGN_CREATED',
        '✅ <b>Кампания создана!</b>\n\n{summary}\n🔗 Ссылка: <code>{link}</code>',
    ).format(summary=summary, link=deep_link)

    await callback.message.edit_text(
        text,
        reply_markup=get_campaign_management_keyboard(campaign.id, campaign.is_active, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def select_campaign_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Обработка выбора тарифа для кампании."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split('_')[-1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(texts.t('CAMPAIGN_TARIFF_NOT_FOUND', '❌ Тариф не найден'), show_alert=True)
        return

    await state.update_data(campaign_tariff_id=tariff_id, campaign_tariff_name=tariff.name)
    await state.set_state(AdminStates.creating_campaign_tariff_days)
    await callback.message.edit_text(
        texts.t(
            'CAMPAIGN_TARIFF_SELECTED_PROMPT',
            '🎁 Выбран тариф: <b>{name}</b>\n\n📅 Введите длительность тарифа в днях (1-730):',
        ).format(name=html.escape(tariff.name)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(
                text=texts.t('CAMPAIGN_BTN_BACK', '⬅️ Назад'), callback_data='admin_campaigns'
            )]]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_campaign_tariff_days(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Обработка ввода длительности тарифа для кампании."""
    texts = get_texts(db_user.language)
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer(texts.t('CAMPAIGN_DAYS_INVALID_INT', '❌ Введите число дней (1-730)'))
        return

    if days <= 0 or days > 730:
        await message.answer(
            texts.t('CAMPAIGN_DAYS_OUT_OF_RANGE', '❌ Длительность должна быть от 1 до 730 дней')
        )
        return

    data = await state.get_data()
    tariff_id = data.get('campaign_tariff_id')

    if not tariff_id:
        await message.answer(
            texts.t('CAMPAIGN_TARIFF_NOT_SELECTED_RESTART', '❌ Тариф не выбран. Начните создание кампании заново.')
        )
        await state.clear()
        return

    campaign = await create_campaign(
        db,
        name=data['campaign_name'],
        start_parameter=data['campaign_start_parameter'],
        bonus_type='tariff',
        tariff_id=tariff_id,
        tariff_duration_days=days,
        created_by=db_user.id,
    )

    # Перезагружаем кампанию с загруженным tariff relationship
    campaign = await get_campaign_by_id(db, campaign.id)

    await state.clear()

    deep_link = await _get_bot_deep_link_from_message(message, campaign.start_parameter)
    summary = _format_campaign_summary(campaign, texts)
    text = texts.t(
        'CAMPAIGN_CREATED',
        '✅ <b>Кампания создана!</b>\n\n{summary}\n🔗 Ссылка: <code>{link}</code>',
    ).format(summary=summary, link=deep_link)

    await message.answer(
        text,
        reply_markup=get_campaign_management_keyboard(campaign.id, campaign.is_active, db_user.language),
    )


@admin_required
@error_handler
async def start_edit_campaign_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Начало редактирования тарифа кампании."""
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    if not campaign.is_tariff_bonus:
        await callback.answer(
            texts.t('CAMPAIGN_TARIFF_NOT_TYPE', "❌ Эта кампания не использует тип 'Тариф'"), show_alert=True
        )
        return

    tariffs = await get_all_tariffs(db, include_inactive=False)
    if not tariffs:
        await callback.answer(texts.t('CAMPAIGN_NO_TARIFFS', '❌ Нет доступных тарифов'), show_alert=True)
        return

    keyboard = []
    for tariff in tariffs[:15]:
        is_current = campaign.tariff_id == tariff.id
        emoji = '✅' if is_current else '🎁'
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'{emoji} {tariff.name}',
                    callback_data=f'campaign_edit_set_tariff_{campaign_id}_{tariff.id}',
                )
            ]
        )
    keyboard.append([types.InlineKeyboardButton(
        text=texts.t('CAMPAIGN_BTN_BACK', '⬅️ Назад'), callback_data=f'admin_campaign_edit_{campaign_id}'
    )])

    current_tariff_name = texts.t('CAMPAIGN_TARIFF_NOT_SELECTED', 'Не выбран')
    if campaign.tariff:
        current_tariff_name = campaign.tariff.name

    await callback.message.edit_text(
        texts.t(
            'CAMPAIGN_EDIT_TARIFF_PROMPT',
            '🎁 <b>Изменение тарифа кампании</b>\n\nТекущий тариф: <b>{tariff}</b>\nВыберите новый тариф:',
        ).format(tariff=current_tariff_name),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def set_campaign_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Установка тарифа для кампании."""
    texts = get_texts(db_user.language)
    parts = callback.data.split('_')
    campaign_id = int(parts[-2])
    tariff_id = int(parts[-1])

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer(texts.t('CAMPAIGN_TARIFF_NOT_FOUND', '❌ Тариф не найден'), show_alert=True)
        return

    await update_campaign(db, campaign, tariff_id=tariff_id)
    await callback.answer(
        texts.t('CAMPAIGN_TARIFF_CHANGED', "✅ Тариф изменён на '{name}'").format(name=tariff.name)
    )

    await _render_campaign_edit_menu(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        campaign,
        db_user.language,
    )


@admin_required
@error_handler
async def start_edit_campaign_tariff_days(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Начало редактирования длительности тарифа."""
    texts = get_texts(db_user.language)
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'), show_alert=True)
        return

    if not campaign.is_tariff_bonus:
        await callback.answer(
            texts.t('CAMPAIGN_TARIFF_NOT_TYPE', "❌ Эта кампания не использует тип 'Тариф'"), show_alert=True
        )
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_tariff_days)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
    )

    await callback.message.edit_text(
        texts.t(
            'CAMPAIGN_EDIT_TARIFF_DAYS_PROMPT',
            '📅 <b>Изменение длительности тарифа</b>\n\n'
            'Текущее значение: <b>{days} д.</b>\n'
            'Введите новое количество дней (1-730):',
        ).format(days=campaign.tariff_duration_days or 0),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CAMPAIGN_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_tariff_days(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Обработка ввода новой длительности тарифа."""
    texts = get_texts(db_user.language)
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer(
            texts.t('CAMPAIGN_EDIT_SESSION_EXPIRED', '❌ Сессия редактирования устарела. Попробуйте снова.')
        )
        await state.clear()
        return

    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer(texts.t('CAMPAIGN_DAYS_INVALID_INT', '❌ Введите число дней (1-730)'))
        return

    if days <= 0 or days > 730:
        await message.answer(
            texts.t('CAMPAIGN_DAYS_OUT_OF_RANGE', '❌ Длительность должна быть от 1 до 730 дней')
        )
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(texts.t('CAMPAIGN_NOT_FOUND', '❌ Кампания не найдена'))
        await state.clear()
        return

    await update_campaign(db, campaign, tariff_duration_days=days)
    await state.clear()

    await message.answer(texts.t('CAMPAIGN_TARIFF_DAYS_UPDATED', '✅ Длительность тарифа обновлена.'))

    edit_message_id = data.get('campaign_edit_message_id')
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
        )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_campaigns_menu, F.data == 'admin_campaigns')
    dp.callback_query.register(show_campaigns_overall_stats, F.data == 'admin_campaigns_stats')
    dp.callback_query.register(show_campaigns_list, F.data == 'admin_campaigns_list')
    dp.callback_query.register(show_campaigns_list, F.data.startswith('admin_campaigns_list_page_'))
    dp.callback_query.register(start_campaign_creation, F.data == 'admin_campaigns_create')
    dp.callback_query.register(show_campaign_stats, F.data.startswith('admin_campaign_stats_'))
    dp.callback_query.register(show_campaign_detail, F.data.startswith('admin_campaign_manage_'))
    dp.callback_query.register(start_edit_campaign_name, F.data.startswith('admin_campaign_edit_name_'))
    dp.callback_query.register(
        start_edit_campaign_start_parameter,
        F.data.startswith('admin_campaign_edit_start_'),
    )
    dp.callback_query.register(
        start_edit_campaign_balance_bonus,
        F.data.startswith('admin_campaign_edit_balance_'),
    )
    dp.callback_query.register(
        start_edit_campaign_subscription_days,
        F.data.startswith('admin_campaign_edit_sub_days_'),
    )
    dp.callback_query.register(
        start_edit_campaign_subscription_traffic,
        F.data.startswith('admin_campaign_edit_sub_traffic_'),
    )
    dp.callback_query.register(
        start_edit_campaign_subscription_devices,
        F.data.startswith('admin_campaign_edit_sub_devices_'),
    )
    dp.callback_query.register(
        start_edit_campaign_subscription_servers,
        F.data.startswith('admin_campaign_edit_sub_servers_'),
    )
    dp.callback_query.register(
        save_edit_campaign_subscription_servers,
        F.data.startswith('campaign_edit_servers_save_'),
    )
    dp.callback_query.register(toggle_edit_campaign_server, F.data.startswith('campaign_edit_toggle_'))
    # Tariff handlers ДОЛЖНЫ быть ПЕРЕД общим admin_campaign_edit_
    dp.callback_query.register(start_edit_campaign_tariff_days, F.data.startswith('admin_campaign_edit_tariff_days_'))
    dp.callback_query.register(start_edit_campaign_tariff, F.data.startswith('admin_campaign_edit_tariff_'))
    # Общий паттерн ПОСЛЕДНИМ
    dp.callback_query.register(show_campaign_edit_menu, F.data.startswith('admin_campaign_edit_'))
    dp.callback_query.register(delete_campaign_confirmed, F.data.startswith('admin_campaign_delete_confirm_'))
    dp.callback_query.register(confirm_delete_campaign, F.data.startswith('admin_campaign_delete_'))
    dp.callback_query.register(toggle_campaign_status, F.data.startswith('admin_campaign_toggle_'))
    dp.callback_query.register(finalize_campaign_subscription, F.data == 'campaign_servers_save')
    dp.callback_query.register(toggle_campaign_server, F.data.startswith('campaign_toggle_server_'))
    dp.callback_query.register(select_campaign_bonus_type, F.data.startswith('campaign_bonus_'))
    dp.callback_query.register(select_campaign_tariff, F.data.startswith('campaign_select_tariff_'))
    dp.callback_query.register(set_campaign_tariff, F.data.startswith('campaign_edit_set_tariff_'))

    dp.message.register(process_campaign_name, AdminStates.creating_campaign_name)
    dp.message.register(process_campaign_start_parameter, AdminStates.creating_campaign_start)
    dp.message.register(process_campaign_balance_value, AdminStates.creating_campaign_balance)
    dp.message.register(
        process_campaign_subscription_days,
        AdminStates.creating_campaign_subscription_days,
    )
    dp.message.register(
        process_campaign_subscription_traffic,
        AdminStates.creating_campaign_subscription_traffic,
    )
    dp.message.register(
        process_campaign_subscription_devices,
        AdminStates.creating_campaign_subscription_devices,
    )
    dp.message.register(process_edit_campaign_name, AdminStates.editing_campaign_name)
    dp.message.register(
        process_edit_campaign_start_parameter,
        AdminStates.editing_campaign_start,
    )
    dp.message.register(
        process_edit_campaign_balance_bonus,
        AdminStates.editing_campaign_balance,
    )
    dp.message.register(
        process_edit_campaign_subscription_days,
        AdminStates.editing_campaign_subscription_days,
    )
    dp.message.register(
        process_edit_campaign_subscription_traffic,
        AdminStates.editing_campaign_subscription_traffic,
    )
    dp.message.register(
        process_edit_campaign_subscription_devices,
        AdminStates.editing_campaign_subscription_devices,
    )
    dp.message.register(
        process_campaign_tariff_days,
        AdminStates.creating_campaign_tariff_days,
    )
    dp.message.register(
        process_edit_campaign_tariff_days,
        AdminStates.editing_campaign_tariff_days,
    )
