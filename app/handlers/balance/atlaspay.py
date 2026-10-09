"""Handler for AtlasPay balance top-up (api.atlaspay.space, Telegram mini-app)."""

import html

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
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

ATLASPAY_PAYMENT_METHODS = {'atlaspay'}

# Telegram caps callback_data at 64 bytes.
_ATLASPAY_CHECK_PREFIX = 'atlaspay_check|'


def _check_topup_restriction(db_user: User, texts) -> InlineKeyboardMarkup | None:
    """Checks the top-up restriction."""
    if not getattr(db_user, 'restriction_topup', False):
        return None

    keyboard = []
    support_url = settings.get_support_contact_url()
    if support_url:
        keyboard.append([InlineKeyboardButton(text='\U0001f198 Обжаловать', url=support_url)])
    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _create_atlaspay_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
):
    """Creates an AtlasPay order and sends the buyer the mini-app pay link."""
    texts = get_texts(db_user.language)

    payment_service = PaymentService()
    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description='Пополнение баланса',
    )

    result = await payment_service.create_atlaspay_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        customer_telegram_id=int(db_user.telegram_id) if db_user.telegram_id else None,
        description=description,
        language=db_user.language,
    )

    if not result or not result.get('payment_url'):
        error_text = texts.t('PAYMENT_CREATE_ERROR', 'Не удалось создать платёж. Попробуйте позже.')
        if edit_message:
            await message_or_callback.edit_text(
                error_text, reply_markup=get_back_keyboard(db_user.language), parse_mode='HTML'
            )
        else:
            await message_or_callback.answer(error_text, parse_mode='HTML')
        return

    payment_url = result['payment_url']
    # Show the PROVIDER total (base + unique delta), never the base amount.
    total_kopeks = result.get('total_amount_kopeks') or amount_kopeks
    total_toman = f'{total_kopeks // 100:,}'.replace(',', '٬')
    # Never show the sequential provider order id — trackingCode only.
    tracking = result.get('tracking_code') or result['order_id']

    pay_button_text = texts.t('ATLASPAY_PAY_BUTTON', '💳 پرداخت')
    check_button_text = texts.t('ATLASPAY_CHECK_BUTTON', '✅ پرداخت کردم')

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=pay_button_text, url=payment_url)],
            [
                InlineKeyboardButton(
                    text=check_button_text,
                    callback_data=f'{_ATLASPAY_CHECK_PREFIX}{result["order_id"]}',
                )
            ],
            [InlineKeyboardButton(text=texts.t('BACK_BUTTON', '◀️ Назад'), callback_data='menu_balance')],
        ]
    )

    response_text = texts.t(
        'ATLASPAY_PAYMENT_CREATED',
        '⚠️ <b>پرداخت از طریق {name}</b>\n\n'
        '💰 مبلغ قابل پرداخت: <b>{total} تومان</b>\n'
        'مبلغ را دقیقاً همین عدد واریز کنید؛ واریز مبلغ رند باعث تأخیر می‌شود.\n\n'
        '⏱ مهلت پرداخت: {minutes} دقیقه\n\n'
        '🌐 از دکمه زیر وارد صفحه پرداخت شوید، پس از واریز رسید را همان‌جا بارگذاری کنید.\n\n'
        '🔖 شماره پیگیری: <code>{tracking}</code>',
    ).format(
        name=settings.get_atlaspay_display_name(),
        total=total_toman,
        minutes=settings.ATLASPAY_DEADLINE_MINUTES,
        tracking=html.escape(str(tracking)),
    )

    if edit_message:
        await message_or_callback.edit_text(response_text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await message_or_callback.answer(response_text, reply_markup=keyboard, parse_mode='HTML')

    logger.info('AtlasPay payment created', telegram_id=db_user.telegram_id, amount_kopeks=amount_kopeks)


@error_handler
async def process_atlaspay_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    """Handles the amount entered by the user for AtlasPay."""
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

    min_amount = settings.ATLASPAY_MIN_AMOUNT_KOPEKS
    max_amount = settings.ATLASPAY_MAX_AMOUNT_KOPEKS

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
            texts.t('ATLASPAY_AMOUNT_MUST_BE_WHOLE', 'مبلغ باید به تومان کامل باشد (بدون خرده).'),
            reply_markup=get_back_keyboard(db_user.language),
            parse_mode='HTML',
        )
        return

    await state.clear()

    await _create_atlaspay_payment_and_respond(
        message_or_callback=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=False,
    )


@error_handler
async def start_atlaspay_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts the amount-entry FSM for AtlasPay."""
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
    await state.update_data(payment_method='atlaspay')

    min_amount = settings.ATLASPAY_MIN_AMOUNT_KOPEKS // 100
    max_amount = settings.ATLASPAY_MAX_AMOUNT_KOPEKS // 100

    display_name = settings.get_atlaspay_display_name()
    keyboard = await get_topup_amount_keyboard('atlaspay', db_user.language)

    await callback.message.edit_text(
        texts.t(
            'ATLASPAY_ENTER_AMOUNT',
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
async def handle_atlaspay_check(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """«پرداخت کردم» — immediate verify via the AtlasPay API."""
    texts = get_texts(db_user.language)
    order_id = (callback.data or '').removeprefix(_ATLASPAY_CHECK_PREFIX)

    service = PaymentService(callback.bot)
    result = await service.check_atlaspay_payment_status(db, order_id)

    if not result:
        await callback.answer(
            texts.t('ATLASPAY_CHECK_ERROR', 'خطا در استعلام وضعیت. دوباره تلاش کنید.'), show_alert=True
        )
        return

    if result.get('is_paid'):
        await state.clear()
        await callback.answer(
            texts.t('ATLASPAY_CHECK_PAID', '✅ پرداخت تأیید و موجودی شارژ شد.'), show_alert=True
        )
        return

    status = (result.get('status') or '').lower()
    if status == 'expired':
        await state.clear()
        await callback.answer(
            texts.t(
                'ATLASPAY_CHECK_EXPIRED',
                '⌛ مهلت پرداخت تمام شد. لطفاً یک پرداخت جدید ایجاد کنید.',
            ),
            show_alert=True,
        )
        return

    if status in ('rejected', 'cancelled', 'failed'):
        await state.clear()
        await callback.answer(
            texts.t(
                'ATLASPAY_CHECK_FAILED',
                '❌ پرداخت رد یا لغو شد. در صورت نیاز یک پرداخت جدید ایجاد کنید.',
            ),
            show_alert=True,
        )
        return

    if status == 'manual_review':
        await state.clear()
        await callback.answer(
            texts.t(
                'ATLASPAY_CHECK_MANUAL',
                '⚠️ پرداخت با مغایرت ثبت شد و در انتظار بررسی پشتیبانی است.',
            ),
            show_alert=True,
        )
        return

    await callback.answer(
        texts.t(
            'ATLASPAY_CHECK_PENDING',
            '⏳ پرداخت هنوز تأیید نشده. کمی بعد دوباره بزنید.',
        ),
        show_alert=True,
    )
