"""CRUD operations for HooshPay payments (hooshpay.xyz/api/v1)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import HooshPayPayment


logger = structlog.get_logger(__name__)


async def create_hooshpay_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'IRT',
    description: str | None = None,
    payment_url: str | None = None,
    hooshpay_payment_id: str | None = None,
    payable_amount_kopeks: int | None = None,
    credited_kopeks: int | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> HooshPayPayment:
    """Creates a HooshPay payment record."""
    payment = HooshPayPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        payable_amount_kopeks=payable_amount_kopeks,
        credited_kopeks=credited_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        hooshpay_payment_id=hooshpay_payment_id,
        expires_at=expires_at,
        metadata_json=metadata_json,
        processed_events=[],
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('HooshPay payment created', order_id=order_id, user_id=user_id)
    return payment


async def get_hooshpay_payment_by_order_id(db: AsyncSession, order_id: str) -> HooshPayPayment | None:
    """Gets a payment by our order_id."""
    result = await db.execute(select(HooshPayPayment).where(HooshPayPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_hooshpay_payment_by_uid(db: AsyncSession, hooshpay_payment_id: str) -> HooshPayPayment | None:
    """Gets a payment by the HooshPay invoice uid."""
    result = await db.execute(
        select(HooshPayPayment).where(HooshPayPayment.hooshpay_payment_id == hooshpay_payment_id)
    )
    return result.scalar_one_or_none()


async def get_hooshpay_payment_by_id(db: AsyncSession, payment_id: int) -> HooshPayPayment | None:
    """Gets a payment by local ID."""
    result = await db.execute(select(HooshPayPayment).where(HooshPayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_hooshpay_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> HooshPayPayment | None:
    """Gets a payment with a FOR UPDATE lock."""
    result = await db.execute(
        select(HooshPayPayment)
        .where(HooshPayPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_hooshpay_payment_status(
    db: AsyncSession,
    payment: HooshPayPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    hooshpay_payment_id: str | None = None,
    payable_amount_kopeks: int | None = None,
    credited_kopeks: int | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> HooshPayPayment:
    """Updates a payment's status."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if hooshpay_payment_id is not None:
        payment.hooshpay_payment_id = hooshpay_payment_id
    if payable_amount_kopeks is not None:
        payment.payable_amount_kopeks = payable_amount_kopeks
    if credited_kopeks is not None:
        payment.credited_kopeks = credited_kopeks
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info('HooshPay payment status updated', order_id=payment.order_id, status=status, is_paid=payment.is_paid)
    return payment


def is_hooshpay_event_processed(payment: HooshPayPayment, event_key: str) -> bool:
    """Whether a (uid, status) webhook event was already handled.

    HooshPay retries delivery on non-200, so idempotency is keyed on the
    concrete event, not on whether the payment is paid.
    """
    return event_key in (payment.processed_events or [])


def remember_hooshpay_event(payment: HooshPayPayment, event_key: str) -> None:
    """Marks a (uid, status) event as handled.

    The list is rebuilt rather than mutated in place: SQLAlchemy tracks JSON
    column changes by assignment, otherwise the UPDATE would be skipped.
    """
    processed = list(payment.processed_events or [])
    if event_key not in processed:
        processed.append(event_key)
    payment.processed_events = processed


async def get_pending_hooshpay_payments(db: AsyncSession, user_id: int) -> list[HooshPayPayment]:
    """Returns the user's unfinished payments."""
    result = await db.execute(
        select(HooshPayPayment).where(
            HooshPayPayment.user_id == user_id,
            HooshPayPayment.status == 'pending',
            HooshPayPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def link_hooshpay_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: HooshPayPayment,
    transaction_id: int,
) -> HooshPayPayment:
    """Links a payment to a transaction."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
