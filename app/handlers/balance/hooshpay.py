"""Handler for HooshPay balance top-up (hooshpay.xyz/api/v1)."""

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

HOOSHPAY_PAYMENT_METHODS = {'hooshpay'}


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


async def _create_hooshpay_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
):
    """Creates a HooshPay invoice and sends the buyer the hosted payment link."""
    texts = get_texts(db_user.language)

    payment_service = PaymentService()
    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description='Пополнение баланса',
    )

    result = await payment_service.create_hooshpay_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
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
    display_name = settings.get_hooshpay_display_name()
    # The buyer must pay payable_amount (invoice + fee + the unique-amount cents);
    # fall back to the requested amount if the provider did not return it.
    payable_kopeks = result.get('payable_amount_kopeks') or amount_kopeks
    payable_toman = payable_kopeks // 100

    pay_button_text = texts.t('PAY_BUTTON', '\U0001f4b3 Оплатить {amount} تومان').format(
        amount=f'{payable_toman:,}'.replace(',', '٬'),
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=pay_button_text, url=payment_url)],
            [InlineKeyboardButton(text=texts.t('BACK_BUTTON', '◀️ Назад'), callback_data='menu_balance')],
        ]
    )

    response_text = texts.t(
        'HOOSHPAY_PAYMENT_CREATED',
        '💳 <b>پرداخت از طریق {name}</b>\n\n'
        'مبلغ قابل پرداخت: <b>{payable} تومان</b>\n\n'
        'روی دکمهٔ زیر بزنید تا به صفحهٔ پرداخت کارت‌به‌کارت بروید.\n'
        'فاکتور تا {minutes} دقیقه معتبر است.\n'
        'موجودی پس از تأیید پرداخت به‌صورت خودکار شارژ می‌شود.',
    ).format(
        name=display_name,
        payable=f'{payable_toman:,}'.replace(',', '٬'),
        minutes=settings.HOOSHPAY_INVOICE_LIFETIME_MINUTES,
    )

    if edit_message:
        await message_or_callback.edit_text(response_text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await message_or_callback.answer(response_text, reply_markup=keyboard, parse_mode='HTML')

    logger.info('HooshPay payment created', telegram_id=db_user.telegram_id, amount_kopeks=amount_kopeks)


@error_handler
async def process_hooshpay_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    """Handles the amount entered by the user for HooshPay."""
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

    min_amount = settings.HOOSHPAY_MIN_AMOUNT_KOPEKS
    max_amount = settings.HOOSHPAY_MAX_AMOUNT_KOPEKS

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

    await state.clear()

    await _create_hooshpay_payment_and_respond(
        message_or_callback=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=False,
    )


@error_handler
async def start_hooshpay_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts the amount-entry FSM for HooshPay."""
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
    await state.update_data(payment_method='hooshpay')

    min_amount = settings.HOOSHPAY_MIN_AMOUNT_KOPEKS // 100
    max_amount = settings.HOOSHPAY_MAX_AMOUNT_KOPEKS // 100

    display_name = settings.get_hooshpay_display_name()
    keyboard = await get_topup_amount_keyboard('hooshpay', db_user.language)

    await callback.message.edit_text(
        texts.t(
            'HOOSHPAY_ENTER_AMOUNT',
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
