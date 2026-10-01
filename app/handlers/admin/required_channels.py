"""Admin handler for managing required channel subscriptions."""

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database.crud.required_channel import (
    add_channel,
    delete_channel,
    get_all_channels,
    get_channel_by_id,
    toggle_channel,
    validate_channel_id,
)
from app.database.database import AsyncSessionLocal
from app.localization.texts import get_texts
from app.services.channel_subscription_service import channel_subscription_service
from app.utils.decorators import admin_required


logger = structlog.get_logger(__name__)

router = Router(name='admin_required_channels')


class AddChannelStates(StatesGroup):
    waiting_channel_id = State()
    waiting_channel_link = State()
    waiting_channel_title = State()


# -- List channels ----------------------------------------------------------------


def _channels_keyboard(channels: list) -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        status = '✅' if ch.is_active else '❌'
        title = ch.title or ch.channel_id
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{status} {title}',
                    callback_data=f'reqch:view:{ch.id}',
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text='➕ Добавить канал', callback_data='reqch:add')])
    buttons.append([InlineKeyboardButton(text='◀️ Назад', callback_data='admin_submenu_settings')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _channel_detail_keyboard(channel_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle_text = '❌ Отключить' if is_active else '✅ Включить'
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data=f'reqch:toggle:{channel_id}')],
            [InlineKeyboardButton(text='🗑 Удалить', callback_data=f'reqch:delete:{channel_id}')],
            [InlineKeyboardButton(text='◀️ К списку', callback_data='reqch:list')],
        ]
    )


@router.callback_query(F.data == 'reqch:list')
@admin_required
async def show_channels_list(callback: CallbackQuery, **kwargs) -> None:
    texts = get_texts(kwargs['db_user'].language)
    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)

    if not channels:
        text = texts.t(
            'ADMIN_REQUIRED_CHANNELS_EMPTY',
            '<b>📢 Обязательные каналы</b>\n\nКаналы не настроены. Нажмите «Добавить» чтобы создать.',
        )
    else:
        lines = [texts.t('ADMIN_REQUIRED_CHANNELS_LIST_HEADER', '<b>📢 Обязательные каналы</b>\n')]
        for ch in channels:
            status = '✅' if ch.is_active else '❌'
            title = ch.title or ch.channel_id
            lines.append(f'{status} <code>{ch.channel_id}</code> — {title}')
        text = '\n'.join(lines)

    await callback.message.edit_text(text, reply_markup=_channels_keyboard(channels))
    await callback.answer()


@router.callback_query(F.data.startswith('reqch:view:'))
@admin_required
async def view_channel(callback: CallbackQuery, **kwargs) -> None:
    texts = get_texts(kwargs['db_user'].language)
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_REQUIRED_CHANNELS_INVALID_ID', 'Неверный ID канала'), show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        ch = await get_channel_by_id(db, channel_db_id)

    if not ch:
        await callback.answer(texts.t('ADMIN_REQUIRED_CHANNELS_NOT_FOUND', 'Канал не найден'), show_alert=True)
        return

    status = (
        texts.t('ADMIN_REQUIRED_CHANNELS_STATUS_ACTIVE', '✅ Активен')
        if ch.is_active
        else texts.t('ADMIN_REQUIRED_CHANNELS_STATUS_INACTIVE', '❌ Отключён')
    )
    text = texts.t(
        'ADMIN_REQUIRED_CHANNELS_DETAIL',
        '<b>{title}</b>\n\n'
        '<b>ID:</b> <code>{channel_id}</code>\n'
        '<b>Ссылка:</b> {link}\n'
        '<b>Статус:</b> {status}\n'
        '<b>Порядок:</b> {sort_order}',
    ).format(
        title=ch.title or texts.t('ADMIN_REQUIRED_CHANNELS_NO_TITLE', 'Без названия'),
        channel_id=ch.channel_id,
        link=ch.channel_link or '—',
        status=status,
        sort_order=ch.sort_order,
    )

    await callback.message.edit_text(text, reply_markup=_channel_detail_keyboard(ch.id, ch.is_active))
    await callback.answer()


# -- Toggle / Delete ---------------------------------------------------------------


@router.callback_query(F.data.startswith('reqch:toggle:'))
@admin_required
async def toggle_channel_handler(callback: CallbackQuery, **kwargs) -> None:
    texts = get_texts(kwargs['db_user'].language)
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_REQUIRED_CHANNELS_INVALID_ID', 'Неверный ID канала'), show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        ch = await toggle_channel(db, channel_db_id)

    if ch:
        await channel_subscription_service.invalidate_channels_cache()
        status = (
            texts.t('ADMIN_REQUIRED_CHANNELS_ENABLED_WORD', 'включён')
            if ch.is_active
            else texts.t('ADMIN_REQUIRED_CHANNELS_DISABLED_WORD', 'отключён')
        )
        await callback.answer(
            texts.t('ADMIN_REQUIRED_CHANNELS_TOGGLED', 'Канал {status}').format(status=status),
            show_alert=True,
        )

    # Refresh list
    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)
    await callback.message.edit_text(
        texts.t('ADMIN_REQUIRED_CHANNELS_TITLE', '<b>📢 Обязательные каналы</b>'),
        reply_markup=_channels_keyboard(channels),
    )


@router.callback_query(F.data.startswith('reqch:delete:'))
@admin_required
async def delete_channel_handler(callback: CallbackQuery, **kwargs) -> None:
    texts = get_texts(kwargs['db_user'].language)
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_REQUIRED_CHANNELS_INVALID_ID', 'Неверный ID канала'), show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        ok = await delete_channel(db, channel_db_id)

    if ok:
        await channel_subscription_service.invalidate_channels_cache()
        await callback.answer(texts.t('ADMIN_REQUIRED_CHANNELS_DELETED', 'Канал удалён'), show_alert=True)
    else:
        await callback.answer(texts.t('ADMIN_REQUIRED_CHANNELS_DELETE_ERROR', 'Ошибка удаления'), show_alert=True)

    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)
    await callback.message.edit_text(
        texts.t('ADMIN_REQUIRED_CHANNELS_TITLE', '<b>📢 Обязательные каналы</b>'),
        reply_markup=_channels_keyboard(channels),
    )


# -- Add channel flow --------------------------------------------------------------


@router.callback_query(F.data == 'reqch:add')
@admin_required
async def start_add_channel(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    texts = get_texts(kwargs['db_user'].language)
    await state.set_state(AddChannelStates.waiting_channel_id)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_REQUIRED_CHANNELS_ADD_PROMPT_ID',
            '<b>➕ Добавить канал</b>\n\n'
            'Отправьте числовой ID канала (например <code>1234567890</code>).\n'
            'Префикс <code>-100</code> добавляется автоматически.',
        )
    )
    await callback.answer()


@router.message(AddChannelStates.waiting_channel_id)
@admin_required
async def process_channel_id(message: Message, state: FSMContext, **kwargs) -> None:
    texts = get_texts(kwargs['db_user'].language)
    if not message.text:
        await message.answer(texts.t('ADMIN_REQUIRED_CHANNELS_SEND_TEXT', 'Отправьте текстовое сообщение.'))
        return
    channel_id = message.text.strip()

    # Validate and normalize channel_id (auto-prefixes -100 for bare digits)
    try:
        channel_id = validate_channel_id(channel_id)
    except ValueError as e:
        await message.answer(
            texts.t(
                'ADMIN_REQUIRED_CHANNELS_INVALID_FORMAT',
                'Неверный формат. {error}\n\nПопробуйте ещё раз:',
            ).format(error=e)
        )
        return

    await state.update_data(channel_id=channel_id)
    await state.set_state(AddChannelStates.waiting_channel_link)
    await message.answer(
        texts.t(
            'ADMIN_REQUIRED_CHANNELS_ADD_PROMPT_LINK',
            'Канал: <code>{channel_id}</code>\n\n'
            'Теперь отправьте ссылку на канал (например <code>https://t.me/mychannel</code>)\n'
            'Или отправьте <code>-</code> чтобы пропустить:',
        ).format(channel_id=channel_id)
    )


@router.message(AddChannelStates.waiting_channel_link)
@admin_required
async def process_channel_link(message: Message, state: FSMContext, **kwargs) -> None:
    texts = get_texts(kwargs['db_user'].language)
    if not message.text:
        await message.answer(texts.t('ADMIN_REQUIRED_CHANNELS_SEND_TEXT', 'Отправьте текстовое сообщение.'))
        return
    link = message.text.strip()
    if link == '-':
        link = None

    if link is not None:
        # Validate and normalize channel link
        if not link.startswith(('https://t.me/', 'http://t.me/', '@')):
            await message.answer(
                texts.t(
                    'ADMIN_REQUIRED_CHANNELS_INVALID_LINK',
                    'Ссылка должна быть URL вида t.me или @username. Попробуйте ещё раз:',
                )
            )
            return
        if link.startswith('@'):
            link = f'https://t.me/{link[1:]}'
        if link.startswith('http://'):
            link = link.replace('http://', 'https://', 1)

    await state.update_data(channel_link=link)
    await state.set_state(AddChannelStates.waiting_channel_title)
    await message.answer(
        texts.t(
            'ADMIN_REQUIRED_CHANNELS_ADD_PROMPT_TITLE',
            'Отправьте название канала (например <code>Новости проекта</code>)\n'
            'Или отправьте <code>-</code> чтобы пропустить:',
        )
    )


@router.message(AddChannelStates.waiting_channel_title)
@admin_required
async def process_channel_title(message: Message, state: FSMContext, **kwargs) -> None:
    texts = get_texts(kwargs['db_user'].language)
    if not message.text:
        await message.answer(texts.t('ADMIN_REQUIRED_CHANNELS_SEND_TEXT', 'Отправьте текстовое сообщение.'))
        return
    title = message.text.strip()
    if title == '-':
        title = None

    data = await state.get_data()
    await state.clear()

    async with AsyncSessionLocal() as db:
        try:
            ch = await add_channel(
                db,
                channel_id=data['channel_id'],
                channel_link=data.get('channel_link'),
                title=title,
            )
            await channel_subscription_service.invalidate_channels_cache()

            text = texts.t(
                'ADMIN_REQUIRED_CHANNELS_ADDED',
                '✅ Канал добавлен!\n\n'
                '<b>ID:</b> <code>{channel_id}</code>\n'
                '<b>Ссылка:</b> {link}\n'
                '<b>Название:</b> {title}',
            ).format(
                channel_id=ch.channel_id,
                link=ch.channel_link or '—',
                title=ch.title or '—',
            )
        except Exception as e:
            text = texts.t('ADMIN_REQUIRED_CHANNELS_ADD_ERROR', '❌ Ошибка добавления канала. Попробуйте ещё раз.')
            logger.error('Error adding channel', error=e)

    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)

    await message.answer(text, reply_markup=_channels_keyboard(channels))


def register_handlers(dp_router: Router) -> None:
    dp_router.include_router(router)
