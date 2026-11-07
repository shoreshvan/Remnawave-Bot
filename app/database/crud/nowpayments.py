"""CRUD operations for NOWPayments payments (api.nowpayments.io)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import NowPaymentsPayment


logger = structlog.get_logger(__name__)


async def create_nowpayments_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'IRT',
    description: str | None = None,
    price_usd: str | None = None,
    nowpayments_payment_id: str | None = None,
    invoice_id: str | None = None,
    payment_url: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> NowPaymentsPayment:
    """Creates a NOWPayments payment record."""
    payment = NowPaymentsPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        price_usd=price_usd,
        currency=currency,
        description=description,
        payment_url=payment_url,
        nowpayments_payment_id=nowpayments_payment_id,
        invoice_id=invoice_id,
        expires_at=expires_at,
        metadata_json=metadata_json,
        processed_events=[],
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('NOWPayments payment created', order_id=order_id, user_id=user_id)
    return payment


async def get_nowpayments_payment_by_order_id(db: AsyncSession, order_id: str) -> NowPaymentsPayment | None:
    """Gets a payment by our order_id."""
    result = await db.execute(select(NowPaymentsPayment).where(NowPaymentsPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_nowpayments_payment_by_provider_id(
    db: AsyncSession, nowpayments_payment_id: str
) -> NowPaymentsPayment | None:
    """Gets a payment by the NOWPayments payment_id."""
    result = await db.execute(
        select(NowPaymentsPayment).where(NowPaymentsPayment.nowpayments_payment_id == nowpayments_payment_id)
    )
    return result.scalar_one_or_none()


async def get_nowpayments_payment_by_id(db: AsyncSession, payment_id: int) -> NowPaymentsPayment | None:
    """Gets a payment by local ID."""
    result = await db.execute(select(NowPaymentsPayment).where(NowPaymentsPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_nowpayments_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> NowPaymentsPayment | None:
    """Gets a payment with a FOR UPDATE lock."""
    result = await db.execute(
        select(NowPaymentsPayment)
        .where(NowPaymentsPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_nowpayments_payment_status(
    db: AsyncSession,
    payment: NowPaymentsPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    nowpayments_payment_id: str | None = None,
    invoice_id: str | None = None,
    pay_currency: str | None = None,
    pay_amount: str | None = None,
    actually_paid: str | None = None,
    outcome_currency: str | None = None,
    outcome_amount: str | None = None,
    payment_url: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> NowPaymentsPayment:
    """Updates a payment's status."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if nowpayments_payment_id is not None:
        payment.nowpayments_payment_id = nowpayments_payment_id
    if invoice_id is not None:
        payment.invoice_id = invoice_id
    if pay_currency is not None:
        payment.pay_currency = pay_currency
    if pay_amount is not None:
        payment.pay_amount = pay_amount
    if actually_paid is not None:
        payment.actually_paid = actually_paid
    if outcome_currency is not None:
        payment.outcome_currency = outcome_currency
    if outcome_amount is not None:
        payment.outcome_amount = outcome_amount
    if payment_url is not None:
        payment.payment_url = payment_url
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'NOWPayments payment status updated', order_id=payment.order_id, status=status, is_paid=payment.is_paid
    )
    return payment


def is_nowpayments_event_processed(payment: NowPaymentsPayment, event_key: str) -> bool:
    """Whether a (payment_id, status) notification was already handled.

    The provider sends an IPN on every status change, so idempotency is keyed
    on the concrete event, not on whether the payment is paid.
    """
    return event_key in (payment.processed_events or [])


def remember_nowpayments_event(payment: NowPaymentsPayment, event_key: str) -> None:
    """Marks a (payment_id, status) event as handled.

    The list is rebuilt rather than mutated in place: SQLAlchemy tracks JSON
    column changes by assignment, otherwise the UPDATE would be skipped.
    """
    processed = list(payment.processed_events or [])
    if event_key not in processed:
        processed.append(event_key)
    payment.processed_events = processed


async def get_pending_nowpayments_payments(db: AsyncSession, user_id: int) -> list[NowPaymentsPayment]:
    """Returns the user's unfinished payments."""
    result = await db.execute(
        select(NowPaymentsPayment).where(
            NowPaymentsPayment.user_id == user_id,
            NowPaymentsPayment.status.in_(['pending', 'processing']),
            NowPaymentsPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def link_nowpayments_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: NowPaymentsPayment,
    transaction_id: int,
) -> NowPaymentsPayment:
    """Links a payment to a transaction."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
