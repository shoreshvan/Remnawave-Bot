import html
from datetime import datetime

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.localization.texts import get_texts
from app.services.backup_service import backup_service
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


class BackupStates(StatesGroup):
    waiting_backup_file = State()
    waiting_settings_update = State()


def get_backup_main_keyboard(language: str = 'ru'):
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('BACKUP_BTN_CREATE', '🚀 Создать бекап'),
                    callback_data='backup_create',
                ),
                InlineKeyboardButton(
                    text=texts.t('BACKUP_BTN_RESTORE', '📥 Восстановить'),
                    callback_data='backup_restore',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('BACKUP_BTN_LIST', '📋 Список бекапов'),
                    callback_data='backup_list',
                ),
                InlineKeyboardButton(
                    text=texts.t('BACKUP_BTN_SETTINGS', '⚙️ Настройки'),
                    callback_data='backup_settings',
                ),
            ],
            [InlineKeyboardButton(text=texts.t('BACKUP_BTN_BACK', '◀️ Назад'), callback_data='admin_panel')],
        ]
    )


def get_backup_list_keyboard(backups: list, page: int = 1, per_page: int = 5):
    keyboard = []

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_backups = backups[start_idx:end_idx]

    for backup in page_backups:
        try:
            if backup.get('timestamp'):
                dt = datetime.fromisoformat(backup['timestamp'].replace('Z', '+00:00'))
                date_str = dt.strftime('%d.%m %H:%M')
            else:
                date_str = '?'
        except:
            date_str = '?'

        size_str = f'{backup.get("file_size_mb", 0):.1f}MB'
        records_str = backup.get('total_records', '?')

        button_text = f'📦 {date_str} • {size_str} • {records_str} записей'
        callback_data = f'backup_manage_{backup["filename"]}'

        keyboard.append([InlineKeyboardButton(text=button_text, callback_data=callback_data)])

    if len(backups) > per_page:
        total_pages = (len(backups) + per_page - 1) // per_page
        nav_row = []

        if page > 1:
            nav_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'backup_list_page_{page - 1}'))

        nav_row.append(InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='noop'))

        if page < total_pages:
            nav_row.append(InlineKeyboardButton(text='➡️', callback_data=f'backup_list_page_{page + 1}'))

        keyboard.append(nav_row)

    keyboard.extend([[InlineKeyboardButton(text='◀️ Назад', callback_data='backup_panel')]])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_backup_manage_keyboard(backup_filename: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='📥 Восстановить', callback_data=f'backup_restore_file_{backup_filename}')],
            [InlineKeyboardButton(text='🗑️ Удалить', callback_data=f'backup_delete_{backup_filename}')],
            [InlineKeyboardButton(text='◀️ К списку', callback_data='backup_list')],
        ]
    )


def get_backup_settings_keyboard(settings_obj):
    auto_status = '✅ Включены' if settings_obj.auto_backup_enabled else '❌ Отключены'
    compression_status = '✅ Включено' if settings_obj.compression_enabled else '❌ Отключено'
    logs_status = '✅ Включены' if settings_obj.include_logs else '❌ Отключены'

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f'🔄 Автобекапы: {auto_status}', callback_data='backup_toggle_auto')],
            [InlineKeyboardButton(text=f'🗜️ Сжатие: {compression_status}', callback_data='backup_toggle_compression')],
            [InlineKeyboardButton(text=f'📋 Логи в бекапе: {logs_status}', callback_data='backup_toggle_logs')],
            [InlineKeyboardButton(text='◀️ Назад', callback_data='backup_panel')],
        ]
    )


@admin_required
@error_handler
async def show_backup_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    settings_obj = await backup_service.get_backup_settings()

    status_auto = (
        texts.t('BACKUP_ENABLED', '✅ Включены')
        if settings_obj.auto_backup_enabled
        else texts.t('BACKUP_DISABLED', '❌ Отключены')
    )

    text = texts.t(
        'BACKUP_PANEL_TEXT',
        (
            '🗄️ <b>СИСТЕМА БЕКАПОВ</b>\n\n'
            '📊 <b>Статус:</b>\n'
            '• Автобекапы: {status_auto}\n'
            '• Интервал: {interval} часов\n'
            '• Хранить: {max_keep} файлов\n'
            '• Сжатие: {compression}\n\n'
            '📁 <b>Расположение:</b> <code>/app/data/backups</code>\n\n'
            '⚡ <b>Доступные операции:</b>\n'
            '• Создание полного бекапа всех данных\n'
            '• Восстановление из файла бекапа\n'
            '• Управление автоматическими бекапами\n'
        ),
    ).format(
        status_auto=status_auto,
        interval=settings_obj.backup_interval_hours,
        max_keep=settings_obj.max_backups_keep,
        compression=texts.t('BACKUP_YES', 'Да') if settings_obj.compression_enabled else texts.t('BACKUP_NO', 'Нет'),
    )

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=get_backup_main_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def create_backup_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('BACKUP_CREATE_STARTED_TOAST', '🔄 Создание бекапа запущено...'))

    progress_msg = await callback.message.edit_text(
        texts.t(
            'BACKUP_CREATE_PROGRESS',
            '🔄 <b>Создание бекапа...</b>\n\n⏳ Экспортируем данные из базы...\nЭто может занять несколько минут.',
        ),
        parse_mode='HTML',
    )

    # Создаем бекап
    created_by_id = db_user.telegram_id or db_user.email or f'#{db_user.id}'
    success, message, file_path = await backup_service.create_backup(created_by=created_by_id, compress=True)

    if success:
        await progress_msg.edit_text(
            texts.t('BACKUP_CREATE_SUCCESS', '✅ <b>Бекап создан успешно!</b>\n\n{message}').format(message=message),
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )
    else:
        await progress_msg.edit_text(
            texts.t('BACKUP_CREATE_FAIL', '❌ <b>Ошибка создания бекапа</b>\n\n{message}').format(
                message=html.escape(message)
            ),
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )


@admin_required
@error_handler
async def show_backup_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    page = 1
    if callback.data.startswith('backup_list_page_'):
        try:
            page = int(callback.data.split('_')[-1])
        except:
            page = 1

    backups = await backup_service.get_backup_list()

    if not backups:
        text = texts.t(
            'BACKUP_LIST_EMPTY',
            '📦 <b>Список бекапов пуст</b>\n\nБекапы еще не создавались.',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('BACKUP_BTN_CREATE_FIRST', '🚀 Создать первый бекап'),
                        callback_data='backup_create',
                    )
                ],
                [InlineKeyboardButton(text=texts.t('BACKUP_BTN_BACK', '◀️ Назад'), callback_data='backup_panel')],
            ]
        )
    else:
        text = texts.t(
            'BACKUP_LIST_HEADER',
            '📦 <b>Список бекапов</b> (всего: {count})\n\n',
        ).format(count=len(backups))
        text += texts.t('BACKUP_LIST_CHOOSE', 'Выберите бекап для управления:')
        keyboard = get_backup_list_keyboard(backups, page)

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def manage_backup_file(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    filename = callback.data.replace('backup_manage_', '')

    backups = await backup_service.get_backup_list()
    backup_info = None

    for backup in backups:
        if backup['filename'] == filename:
            backup_info = backup
            break

    if not backup_info:
        await callback.answer(texts.t('BACKUP_FILE_MISSING_TOAST', '❌ Файл бекапа не найден'), show_alert=True)
        return

    try:
        if backup_info.get('timestamp'):
            dt = datetime.fromisoformat(backup_info['timestamp'].replace('Z', '+00:00'))
            date_str = dt.strftime('%d.%m.%Y %H:%M:%S')
        else:
            date_str = texts.t('BACKUP_DATE_UNKNOWN', 'Неизвестно')
    except:
        date_str = texts.t('BACKUP_DATE_FORMAT_ERROR', 'Ошибка формата даты')

    text = texts.t(
        'BACKUP_INFO_TEXT',
        '📦 <b>Информация о бекапе</b>\n\n'
        '📄 <b>Файл:</b> <code>{filename}</code>\n'
        '📅 <b>Создан:</b> {date_str}\n'
        '💾 <b>Размер:</b> {size_mb:.2f} MB\n'
        '📊 <b>Таблиц:</b> {tables_count}\n'
        '📈 <b>Записей:</b> {total_records:,}\n'
        '🗜️ <b>Сжатие:</b> {compression}\n'
        '🗄️ <b>БД:</b> {database_type}\n',
    ).format(
        filename=filename,
        date_str=date_str,
        size_mb=backup_info.get('file_size_mb', 0),
        tables_count=backup_info.get('tables_count', '?'),
        total_records=backup_info.get('total_records', '?'),
        compression=texts.t('BACKUP_YES', 'Да') if backup_info.get('compressed') else texts.t('BACKUP_NO', 'Нет'),
        database_type=backup_info.get('database_type', 'unknown'),
    )

    if backup_info.get('error'):
        text += texts.t('BACKUP_INFO_ERROR_SUFFIX', '\n⚠️ <b>Ошибка:</b> {error}').format(error=backup_info['error'])

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=get_backup_manage_keyboard(filename))
    await callback.answer()


@admin_required
@error_handler
async def delete_backup_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    filename = callback.data.replace('backup_delete_', '')

    text = texts.t('BACKUP_DELETE_CONFIRM_TITLE', '🗑️ <b>Удаление бекапа</b>\n\n')
    text += texts.t('BACKUP_DELETE_CONFIRM_ASK', 'Вы уверены, что хотите удалить бекап?\n\n')
    text += texts.t('BACKUP_DELETE_CONFIRM_FILE', '📄 <code>{filename}</code>\n\n').format(filename=filename)
    text += texts.t('BACKUP_DELETE_CONFIRM_WARN', '⚠️ <b>Это действие нельзя отменить!</b>')

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('BACKUP_BTN_DELETE_YES', '✅ Да, удалить'),
                    callback_data=f'backup_delete_confirm_{filename}',
                ),
                InlineKeyboardButton(
                    text=texts.t('BACKUP_BTN_CANCEL', '❌ Отмена'),
                    callback_data=f'backup_manage_{filename}',
                ),
            ]
        ]
    )

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def delete_backup_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    filename = callback.data.replace('backup_delete_confirm_', '')

    success, message = await backup_service.delete_backup(filename)

    if success:
        await callback.message.edit_text(
            texts.t('BACKUP_DELETE_SUCCESS', '✅ <b>Бекап удален</b>\n\n{message}').format(message=message),
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('BACKUP_BTN_TO_LIST', '📋 К списку бекапов'),
                            callback_data='backup_list',
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('BACKUP_DELETE_FAIL', '❌ <b>Ошибка удаления</b>\n\n{message}').format(message=message),
            parse_mode='HTML',
            reply_markup=get_backup_manage_keyboard(filename),
        )

    await callback.answer()


@admin_required
@error_handler
async def restore_backup_start(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    if callback.data.startswith('backup_restore_file_'):
        # Восстановление из конкретного файла
        filename = callback.data.replace('backup_restore_file_', '')

        text = texts.t('BACKUP_RESTORE_TITLE', '📥 <b>Восстановление из бекапа</b>\n\n')
        text += texts.t('BACKUP_RESTORE_FILE_LINE', '📄 <b>Файл:</b> <code>{filename}</code>\n\n').format(
            filename=filename
        )
        text += texts.t('BACKUP_RESTORE_WARN_TITLE', '⚠️ <b>ВНИМАНИЕ!</b>\n')
        text += texts.t('BACKUP_RESTORE_WARN_TIME', '• Процесс может занять несколько минут\n')
        text += texts.t('BACKUP_RESTORE_WARN_RECOMMEND', '• Рекомендуется создать бекап перед восстановлением\n')
        text += texts.t('BACKUP_RESTORE_WARN_APPEND', '• Существующие данные будут дополнены\n\n')
        text += texts.t('BACKUP_RESTORE_CONFIRM_Q', 'Продолжить восстановление?')

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('BACKUP_BTN_RESTORE_YES', '✅ Да, восстановить'),
                        callback_data=f'backup_restore_execute_{filename}',
                    ),
                    InlineKeyboardButton(
                        text=texts.t('BACKUP_BTN_RESTORE_CLEAR', '🗑️ Очистить и восстановить'),
                        callback_data=f'backup_restore_clear_{filename}',
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BACKUP_BTN_CANCEL', '❌ Отмена'),
                        callback_data=f'backup_manage_{filename}',
                    )
                ],
            ]
        )
    else:
        text = texts.t(
            'BACKUP_RESTORE_UPLOAD_PROMPT',
            '📥 <b>Восстановление из бекапа</b>\n\n'
            '📎 Отправьте файл бекапа (.json, .json.gz или .tar.gz)\n\n'
            '⚠️ <b>ВАЖНО:</b>\n'
            '• Файл должен быть создан этой системой бекапов\n'
            '• Процесс может занять несколько минут\n'
            '• Рекомендуется создать бекап перед восстановлением\n\n'
            '💡 Или выберите из существующих бекапов ниже.',
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('BACKUP_BTN_CHOOSE_FROM_LIST', '📋 Выбрать из списка'),
                        callback_data='backup_list',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BACKUP_BTN_CANCEL', '❌ Отмена'),
                        callback_data='backup_panel',
                    )
                ],
            ]
        )

        await state.set_state(BackupStates.waiting_backup_file)

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def restore_backup_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    if callback.data.startswith('backup_restore_execute_'):
        filename = callback.data.replace('backup_restore_execute_', '')
        clear_existing = False
    elif callback.data.startswith('backup_restore_clear_'):
        filename = callback.data.replace('backup_restore_clear_', '')
        clear_existing = True
    else:
        await callback.answer(texts.t('BACKUP_INVALID_COMMAND_TOAST', '❌ Неверный формат команды'), show_alert=True)
        return

    await callback.answer(texts.t('BACKUP_RESTORE_STARTED_TOAST', '🔄 Восстановление запущено...'))

    # Показываем прогресс
    action_text = (
        texts.t('BACKUP_RESTORE_ACTION_CLEAR', 'очисткой и восстановлением')
        if clear_existing
        else texts.t('BACKUP_RESTORE_ACTION_NORMAL', 'восстановлением')
    )
    progress_msg = await callback.message.edit_text(
        texts.t(
            'BACKUP_RESTORE_PROGRESS',
            '📥 <b>Восстановление из бекапа...</b>\n\n'
            '⏳ Работаем с {action_text} данных...\n'
            '📄 Файл: <code>{filename}</code>\n\n'
            'Это может занять несколько минут.',
        ).format(action_text=action_text, filename=filename),
        parse_mode='HTML',
    )

    backup_path = backup_service.backup_dir / filename

    success, message = await backup_service.restore_backup(str(backup_path), clear_existing=clear_existing)

    if success:
        await progress_msg.edit_text(
            texts.t('BACKUP_RESTORE_SUCCESS', '✅ <b>Восстановление завершено!</b>\n\n{message}').format(
                message=message
            ),
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )
    else:
        await progress_msg.edit_text(
            texts.t('BACKUP_RESTORE_FAIL', '❌ <b>Ошибка восстановления</b>\n\n{message}').format(message=message),
            parse_mode='HTML',
            reply_markup=get_backup_manage_keyboard(filename),
        )


@admin_required
@error_handler
async def handle_backup_file_upload(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    if not message.document:
        await message.answer(
            texts.t('BACKUP_UPLOAD_NO_DOCUMENT', '❌ Пожалуйста, отправьте файл бекапа (.json, .json.gz или .tar.gz)'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('BACKUP_BTN_CANCEL_BACK', '◀️ Отмена'),
                            callback_data='backup_panel',
                        )
                    ]
                ]
            ),
        )
        return

    document = message.document
    allowed_extensions = ('.json', '.json.gz', '.tar.gz', '.tar')

    if not document.file_name or not any(document.file_name.endswith(ext) for ext in allowed_extensions):
        await message.answer(
            texts.t(
                'BACKUP_UPLOAD_BAD_FORMAT',
                '❌ Неподдерживаемый формат файла. Загрузите .json, .json.gz или .tar.gz файл',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('BACKUP_BTN_CANCEL_BACK', '◀️ Отмена'),
                            callback_data='backup_panel',
                        )
                    ]
                ]
            ),
        )
        return

    if document.file_size > 50 * 1024 * 1024:
        await message.answer(
            texts.t('BACKUP_UPLOAD_TOO_LARGE', '❌ Файл слишком большой (максимум 50MB)'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('BACKUP_BTN_CANCEL_BACK', '◀️ Отмена'),
                            callback_data='backup_panel',
                        )
                    ]
                ]
            ),
        )
        return

    try:
        file = await message.bot.get_file(document.file_id)

        temp_path = backup_service.backup_dir / f'uploaded_{document.file_name}'

        await message.bot.download_file(file.file_path, temp_path)

        text = texts.t(
            'BACKUP_UPLOAD_LOADED_TEXT',
            '📥 <b>Файл загружен</b>\n\n'
            '📄 <b>Имя:</b> <code>{file_name}</code>\n'
            '💾 <b>Размер:</b> {size_mb:.2f} MB\n\n'
            '⚠️ <b>ВНИМАНИЕ!</b>\n'
            'Процесс восстановления изменит данные в базе.\n'
            'Рекомендуется создать бекап перед восстановлением.\n\n'
            'Продолжить?',
        ).format(file_name=document.file_name, size_mb=document.file_size / 1024 / 1024)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('BACKUP_BTN_RESTORE_DO', '✅ Восстановить'),
                        callback_data=f'backup_restore_execute_{temp_path.name}',
                    ),
                    InlineKeyboardButton(
                        text=texts.t('BACKUP_BTN_RESTORE_CLEAR', '🗑️ Очистить и восстановить'),
                        callback_data=f'backup_restore_clear_{temp_path.name}',
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BACKUP_BTN_CANCEL', '❌ Отмена'),
                        callback_data='backup_panel',
                    )
                ],
            ]
        )

        await message.answer(text, parse_mode='HTML', reply_markup=keyboard)
        await state.clear()

    except Exception as e:
        logger.error('Ошибка загрузки файла бекапа', error=e)
        await message.answer(
            texts.t('BACKUP_UPLOAD_ERROR', '❌ Ошибка загрузки файла: {error!s}').format(error=e),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('BACKUP_BTN_CANCEL_BACK', '◀️ Отмена'),
                            callback_data='backup_panel',
                        )
                    ]
                ]
            ),
        )


@admin_required
@error_handler
async def show_backup_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    settings_obj = await backup_service.get_backup_settings()

    text = texts.t(
        'BACKUP_SETTINGS_TEXT',
        '⚙️ <b>Настройки системы бекапов</b>\n\n'
        '🔄 <b>Автоматические бекапы:</b>\n'
        '• Статус: {status}\n'
        '• Интервал: {interval} часов\n'
        '• Время запуска: {backup_time}\n\n'
        '📦 <b>Хранение:</b>\n'
        '• Максимум файлов: {max_keep}\n'
        '• Сжатие: {compression}\n'
        '• Включать логи: {logs}\n\n'
        '📁 <b>Расположение:</b> <code>{location}</code>\n',
    ).format(
        status=(
            texts.t('BACKUP_ENABLED', '✅ Включены')
            if settings_obj.auto_backup_enabled
            else texts.t('BACKUP_DISABLED', '❌ Отключены')
        ),
        interval=settings_obj.backup_interval_hours,
        backup_time=settings_obj.backup_time,
        max_keep=settings_obj.max_backups_keep,
        compression=(
            texts.t('BACKUP_ENABLED_N', '✅ Включено')
            if settings_obj.compression_enabled
            else texts.t('BACKUP_DISABLED_N', '❌ Отключено')
        ),
        logs=(
            texts.t('BACKUP_YES_MARK', '✅ Да')
            if settings_obj.include_logs
            else texts.t('BACKUP_NO_MARK', '❌ Нет')
        ),
        location=settings_obj.backup_location,
    )

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=get_backup_settings_keyboard(settings_obj))
    await callback.answer()


@admin_required
@error_handler
async def toggle_backup_setting(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    settings_obj = await backup_service.get_backup_settings()

    if callback.data == 'backup_toggle_auto':
        new_value = not settings_obj.auto_backup_enabled
        await backup_service.update_backup_settings(auto_backup_enabled=new_value)
        status = (
            texts.t('BACKUP_STATUS_ON_PL', 'включены') if new_value else texts.t('BACKUP_STATUS_OFF_PL', 'отключены')
        )
        await callback.answer(texts.t('BACKUP_TOAST_AUTO', 'Автобекапы {status}').format(status=status))

    elif callback.data == 'backup_toggle_compression':
        new_value = not settings_obj.compression_enabled
        await backup_service.update_backup_settings(compression_enabled=new_value)
        status = (
            texts.t('BACKUP_STATUS_ON_N', 'включено') if new_value else texts.t('BACKUP_STATUS_OFF_N', 'отключено')
        )
        await callback.answer(texts.t('BACKUP_TOAST_COMPRESSION', 'Сжатие {status}').format(status=status))

    elif callback.data == 'backup_toggle_logs':
        new_value = not settings_obj.include_logs
        await backup_service.update_backup_settings(include_logs=new_value)
        status = (
            texts.t('BACKUP_STATUS_ON_PL', 'включены') if new_value else texts.t('BACKUP_STATUS_OFF_PL', 'отключены')
        )
        await callback.answer(texts.t('BACKUP_TOAST_LOGS', 'Логи в бекапе {status}').format(status=status))

    await show_backup_settings(callback, db_user, db)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_backup_panel, F.data == 'backup_panel')

    dp.callback_query.register(create_backup_handler, F.data == 'backup_create')

    dp.callback_query.register(show_backup_list, F.data.startswith('backup_list'))

    dp.callback_query.register(manage_backup_file, F.data.startswith('backup_manage_'))

    dp.callback_query.register(
        delete_backup_confirm, F.data.startswith('backup_delete_') & ~F.data.startswith('backup_delete_confirm_')
    )

    dp.callback_query.register(delete_backup_execute, F.data.startswith('backup_delete_confirm_'))

    dp.callback_query.register(
        restore_backup_start, F.data.in_(['backup_restore']) | F.data.startswith('backup_restore_file_')
    )

    dp.callback_query.register(
        restore_backup_execute,
        F.data.startswith('backup_restore_execute_') | F.data.startswith('backup_restore_clear_'),
    )

    dp.callback_query.register(show_backup_settings, F.data == 'backup_settings')

    dp.callback_query.register(
        toggle_backup_setting, F.data.in_(['backup_toggle_auto', 'backup_toggle_compression', 'backup_toggle_logs'])
    )

    dp.message.register(handle_backup_file_upload, BackupStates.waiting_backup_file)
