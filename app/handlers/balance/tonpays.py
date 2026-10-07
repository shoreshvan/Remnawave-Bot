"""Handler for TonPays balance top-up (tonpays.online custom gateway, Telegram platform)."""

import html

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.keyboards.topup_amounts import get_topup_amount_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

TONPAYS_PAYMENT_METHODS = {'tonpays'}

# Telegram caps callback_data at 64 bytes; our order_id is 14 chars.
_TONPAYS_CHECK_PREFIX = 'tonpays_check|'
_TONPAYS_NEWCARD_PREFIX = 'tonpays_card|'


def _check_topup_restriction(db_user: User, texts):
    """Checks the top-up restriction."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    if not getattr(db_user, 'restriction_topup', False):
        return None

    keyboard = []
    support_url = settings.get_support_contact_url()
    if support_url:
        keyboard.append([InlineKeyboardButton(text='\U0001f198 Обжаловать', url=support_url)])
    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _render_card_text(
    texts,
    *,
    display_name: str,
    card_number: str | None,
    card_name: str | None,
    final_toman: str,
    minutes: int,
) -> str:
    return texts.t(
        'TONPAYS_CARD_MESSAGE',
        '💳 <b>پرداخت از طریق {name}</b>\n\n'
        'مبلغ قابل پرداخت: <b>{payable} تومان</b>\n'
        '💳 شماره کارت:\n<code>{card}</code>\n'
        '👤 به نام: {holder}\n\n'
        'مبلغ دقیق را کارت‌به‌کارت کنید، سپس فیش را همین‌جا بفرستید\n'
        'یا دکمه «پرداخت کردم» را بزنید.\n'
        'فاکتور تا {minutes} دقیقه معتبر است.',
    ).format(
        name=display_name,
        payable=final_toman,
        card=html.escape(card_number or '—'),
        holder=html.escape(card_name or '—'),
        minutes=minutes,
    )


def _card_keyboard(texts, order_id: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('TONPAYS_PAID_BUTTON', '✅ پرداخت کردم'),
                    callback_data=f'{_TONPAYS_CHECK_PREFIX}{order_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('TONPAYS_NEW_CARD_BUTTON', '🔄 تعویض کارت'),
                    callback_data=f'{_TONPAYS_NEWCARD_PREFIX}{order_id}',
                )
            ],
            [InlineKeyboardButton(text=texts.t('BACK_BUTTON', '◀️ Назад'), callback_data='menu_balance')],
        ]
    )


async def _create_tonpays_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
    edit_message: bool = False,
):
    """Creates a TonPays invoice and shows the buyer the card details."""
    texts = get_texts(db_user.language)

    if not db_user.telegram_id:
        error_text = texts.t(
            'TONPAYS_NO_TELEGRAM', 'این روش پرداخت نیاز به حساب تلگرام دارد.'
        )
        if edit_message:
            await message_or_callback.edit_text(
                error_text, reply_markup=get_back_keyboard(db_user.language), parse_mode='HTML'
            )
        else:
            await message_or_callback.answer(error_text, parse_mode='HTML')
        return

    payment_service = PaymentService()
    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description='Пополнение баланса',
    )

    result = await payment_service.create_tonpays_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        buyer_chat_id=int(db_user.telegram_id),
        description=description,
        language=db_user.language,
    )

    if not result or not result.get('invoice_id'):
        error_text = texts.t('PAYMENT_CREATE_ERROR', 'Не удалось создать платёж. Попробуйте позже.')
        if edit_message:
            await message_or_callback.edit_text(
                error_text, reply_markup=get_back_keyboard(db_user.language), parse_mode='HTML'
            )
        else:
            await message_or_callback.answer(error_text, parse_mode='HTML')
        return

    display_name = settings.get_tonpays_display_name()
    final_kopeks = result.get('final_amount_kopeks') or amount_kopeks
    final_toman = f'{final_kopeks // 100:,}'.replace(',', '٬')
    order_id = result['order_id']

    response_text = _render_card_text(
        texts,
        display_name=display_name,
        card_number=result.get('card_number'),
        card_name=result.get('card_name'),
        final_toman=final_toman,
        minutes=settings.TONPAYS_INVOICE_LIFETIME_MINUTES,
    )
    keyboard = _card_keyboard(texts, order_id)

    if edit_message:
        await message_or_callback.edit_text(response_text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await message_or_callback.answer(response_text, reply_markup=keyboard, parse_mode='HTML')

    # From now on the user may send the receipt photo for this invoice.
    await state.set_state(BalanceStates.waiting_for_tonpays_receipt)
    await state.update_data(payment_method='tonpays', tonpays_order_id=order_id)

    logger.info('TonPays payment created', telegram_id=db_user.telegram_id, amount_kopeks=amount_kopeks)


@error_handler
async def process_tonpays_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    """Handles the amount entered by the user for TonPays."""
    texts = get_texts(db_user.language)

    restriction_kb = _check_topup_restriction(db_user, texts)
    if restriction_kb:
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        await message.answer(
            f'\U0001f6ab <b>Пополнение ограничено</b>\n\n{reason}',
            parse_mode='HTML',
            reply_markup=restriction_kb,
        )
        await state.clear()
        return

    min_amount = settings.TONPAYS_MIN_AMOUNT_KOPEKS
    max_amount = settings.TONPAYS_MAX_AMOUNT_KOPEKS

    if amount_kopeks < min_amount:
        await message.answer(
            texts.t('PAYMENT_AMOUNT_TOO_LOW', 'Минимальная сумма пополнения: {min_amount} تومان').format(
                min_amount=min_amount // 100
            ),
            reply_markup=get_back_keyboard(db_user.language),
            parse_mode='HTML',
        )
        return

    if amount_kopeks > max_amount:
        await message.answer(
            texts.t('PAYMENT_AMOUNT_TOO_HIGH', 'Максимальная сумма пополнения: {max_amount} تومان').format(
                max_amount=max_amount // 100
            ),
            reply_markup=get_back_keyboard(db_user.language),
            parse_mode='HTML',
        )
        return

    if amount_kopeks % 100 != 0:
        await message.answer(
            texts.t('TONPAYS_AMOUNT_MUST_BE_WHOLE', 'مبلغ باید به تومان کامل باشد (بدون خرده).'),
            reply_markup=get_back_keyboard(db_user.language),
            parse_mode='HTML',
        )
        return

    await state.clear()

    await _create_tonpays_payment_and_respond(
        message_or_callback=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        state=state,
        edit_message=False,
    )


@error_handler
async def start_tonpays_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts the amount-entry FSM for TonPays."""
    texts = get_texts(db_user.language)

    restriction_kb = _check_topup_restriction(db_user, texts)
    if restriction_kb:
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        await callback.message.edit_text(
            f'\U0001f6ab <b>Пополнение ограничено</b>\n\n{reason}',
            parse_mode='HTML',
            reply_markup=restriction_kb,
        )
        return

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='tonpays')

    min_amount = settings.TONPAYS_MIN_AMOUNT_KOPEKS // 100
    max_amount = settings.TONPAYS_MAX_AMOUNT_KOPEKS // 100

    display_name = settings.get_tonpays_display_name()
    keyboard = await get_topup_amount_keyboard('tonpays', db_user.language)

    await callback.message.edit_text(
        texts.t(
            'TONPAYS_ENTER_AMOUNT',
            '💳 <b>پرداخت از طریق {name}</b>\n\n'
            'مبلغ شارژ را به تومان وارد کنید.\n\n'
            'حداقل: {min_amount} تومان\n'
            'حداکثر: {max_amount} تومان',
        ).format(
            name=display_name,
            min_amount=f'{min_amount:,}'.replace(',', '٬'),
            max_amount=f'{max_amount:,}'.replace(',', '٬'),
        ),
        parse_mode='HTML',
        reply_markup=keyboard,
    )


@error_handler
async def handle_tonpays_check(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """«پرداخت کردم» — immediate status check via the TonPays API."""
    texts = get_texts(db_user.language)
    order_id = (callback.data or '').removeprefix(_TONPAYS_CHECK_PREFIX)

    service = PaymentService(callback.bot)
    result = await service.check_tonpays_payment_status(db, order_id)

    if not result:
        await callback.answer(
            texts.t('TONPAYS_CHECK_ERROR', 'خطا در استعلام وضعیت. دوباره تلاش کنید.'), show_alert=True
        )
        return

    if result.get('is_paid'):
        await state.clear()
        await callback.answer(
            texts.t('TONPAYS_CHECK_PAID', '✅ پرداخت تأیید و موجودی شارژ شد.'), show_alert=True
        )
        return

    status = (result.get('status') or '').lower()
    if status == 'expired':
        await state.clear()
        await callback.answer(
            texts.t(
                'TONPAYS_CHECK_EXPIRED',
                '⌛ مهلت پرداخت تمام شد. لطفاً یک پرداخت جدید ایجاد کنید.',
            ),
            show_alert=True,
        )
        return

    if status in ('rejected', 'cancelled', 'failed'):
        await state.clear()
        await callback.answer(
            texts.t(
                'TONPAYS_CHECK_FAILED',
                '❌ پرداخت رد یا لغو شد. در صورت نیاز یک پرداخت جدید ایجاد کنید.',
            ),
            show_alert=True,
        )
        return

    await callback.answer(
        texts.t(
            'TONPAYS_CHECK_PENDING', '⏳ پرداخت هنوز تأیید نشده. پس از واریز، فیش را بفرستید یا کمی بعد دوباره بزنید.'
        ),
        show_alert=True,
    )


@error_handler
async def handle_tonpays_new_card(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """تعویض کارت — new card from TonPays with a local 60s cooldown."""
    texts = get_texts(db_user.language)
    order_id = (callback.data or '').removeprefix(_TONPAYS_NEWCARD_PREFIX)

    service = PaymentService(callback.bot)
    result = await service.change_tonpays_card(db, order_id)

    if not result:
        await callback.answer(
            texts.t('TONPAYS_NEW_CARD_ERROR', 'تعویض کارت ناموفق بود. کمی بعد تلاش کنید.'), show_alert=True
        )
        return

    if result.get('cooldown_wait'):
        await callback.answer(
            texts.t('TONPAYS_NEW_CARD_COOLDOWN', '⏳ لطفاً {seconds} ثانیه صبر کنید.').format(
                seconds=result['cooldown_wait']
            ),
            show_alert=True,
        )
        return

    payment = result['payment']
    display_name = settings.get_tonpays_display_name()
    final_kopeks = payment.final_amount_kopeks or payment.amount_kopeks
    response_text = _render_card_text(
        texts,
        display_name=display_name,
        card_number=payment.card_number,
        card_name=payment.card_name,
        final_toman=f'{final_kopeks // 100:,}'.replace(',', '٬'),
        minutes=settings.TONPAYS_INVOICE_LIFETIME_MINUTES,
    )
    try:
        await callback.message.edit_text(
            response_text, reply_markup=_card_keyboard(texts, order_id), parse_mode='HTML'
        )
    except Exception:
        await callback.message.answer(response_text, reply_markup=_card_keyboard(texts, order_id), parse_mode='HTML')
    await callback.answer()

    if result.get('change_card_exhausted'):
        logger.warning('TonPays cards exhausted for invoice', order_id=order_id)


@error_handler
async def process_tonpays_receipt(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Receives the receipt photo and uploads it to TonPays."""
    texts = get_texts(db_user.language)

    data = await state.get_data()
    order_id = data.get('tonpays_order_id')
    if not order_id:
        return
    if not message.photo:
        # Let commands pass through to their own handlers.
        if message.text and message.text.startswith('/'):
            return
        await message.answer(
            texts.t('TONPAYS_PHOTO_HINT', 'لطفاً عکس فیش واریزی را همین‌جا بفرستید.')
        )
        return

    photo = message.photo[-1]
    if photo.file_size and photo.file_size > settings.TONPAYS_RECEIPT_MAX_BYTES:
        await message.answer(
            texts.t('TONPAYS_RECEIPT_TOO_LARGE', 'حجم فیش بیش از حد مجاز است (حداکثر ۱۰ مگابایت).')
        )
        return

    try:
        downloaded = await message.bot.download(photo.file_id)
        file_bytes = downloaded.read() if hasattr(downloaded, 'read') else bytes(downloaded)
    except Exception as error:
        logger.error('TonPays receipt download error', error=error)
        await message.answer(texts.t('TONPAYS_RECEIPT_ERROR', 'دریافت فیش ناموفق بود. دوباره بفرستید.'))
        return

    if len(file_bytes) > settings.TONPAYS_RECEIPT_MAX_BYTES:
        await message.answer(
            texts.t('TONPAYS_RECEIPT_TOO_LARGE', 'حجم فیش بیش از حد مجاز است (حداکثر ۱۰ مگابایت).')
        )
        return

    service = PaymentService(message.bot)
    result = await service.upload_tonpays_receipt(
        db, order_id, file_bytes=file_bytes, filename='receipt.jpg'
    )
    if not result:
        await message.answer(
            texts.t('TONPAYS_RECEIPT_FAILED', 'ارسال فیش ناموفق بود. کمی بعد دوباره تلاش کنید.')
        )
        return

    await message.answer(
        texts.t(
            'TONPAYS_RECEIPT_OK',
            '✅ فیش دریافت شد و برای تأیید ارسال شد. پس از تأیید، موجودی به‌صورت خودکار شارژ می‌شود.',
        )
    )
    logger.info('TonPays receipt uploaded', telegram_id=db_user.telegram_id, order_id=order_id)
