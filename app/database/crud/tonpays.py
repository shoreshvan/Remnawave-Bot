"""CRUD operations for TonPays payments (tonpays.online custom gateway)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TonPaysPayment


logger = structlog.get_logger(__name__)


async def create_tonpays_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'IRT',
    description: str | None = None,
    tonpays_payment_id: str | None = None,
    final_amount_kopeks: int | None = None,
    card_number: str | None = None,
    card_name: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> TonPaysPayment:
    """Creates a TonPays payment record."""
    payment = TonPaysPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        final_amount_kopeks=final_amount_kopeks,
        currency=currency,
        description=description,
        card_number=card_number,
        card_name=card_name,
        tonpays_payment_id=tonpays_payment_id,
        expires_at=expires_at,
        metadata_json=metadata_json,
        processed_events=[],
        status='pending',
        is_paid=False,
        receipt_received=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('TonPays payment created', order_id=order_id, user_id=user_id)
    return payment


async def get_tonpays_payment_by_order_id(db: AsyncSession, order_id: str) -> TonPaysPayment | None:
    """Gets a payment by our order_id."""
    result = await db.execute(select(TonPaysPayment).where(TonPaysPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_tonpays_payment_by_invoice_id(db: AsyncSession, tonpays_payment_id: str) -> TonPaysPayment | None:
    """Gets a payment by the TonPays invoice_id."""
    result = await db.execute(
        select(TonPaysPayment).where(TonPaysPayment.tonpays_payment_id == tonpays_payment_id)
    )
    return result.scalar_one_or_none()


async def get_tonpays_payment_by_id(db: AsyncSession, payment_id: int) -> TonPaysPayment | None:
    """Gets a payment by local ID."""
    result = await db.execute(select(TonPaysPayment).where(TonPaysPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_tonpays_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> TonPaysPayment | None:
    """Gets a payment with a FOR UPDATE lock."""
    result = await db.execute(
        select(TonPaysPayment)
        .where(TonPaysPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_tonpays_payment_status(
    db: AsyncSession,
    payment: TonPaysPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    tonpays_payment_id: str | None = None,
    final_amount_kopeks: int | None = None,
    credit_amount_kopeks: int | None = None,
    card_number: str | None = None,
    card_name: str | None = None,
    receipt_received: bool | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> TonPaysPayment:
    """Updates a payment's status."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if tonpays_payment_id is not None:
        payment.tonpays_payment_id = tonpays_payment_id
    if final_amount_kopeks is not None:
        payment.final_amount_kopeks = final_amount_kopeks
    if credit_amount_kopeks is not None:
        payment.credit_amount_kopeks = credit_amount_kopeks
    if card_number is not None:
        payment.card_number = card_number
    if card_name is not None:
        payment.card_name = card_name
    if receipt_received is not None:
        payment.receipt_received = receipt_received
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info('TonPays payment status updated', order_id=payment.order_id, status=status, is_paid=payment.is_paid)
    return payment


def is_tonpays_event_processed(payment: TonPaysPayment, event_key: str) -> bool:
    """Whether a webhook delivery was already handled.

    TonPays retries delivery on non-200, so idempotency is keyed on the
    provider ``delivery_id`` (``invoice:status:timestamp``), falling back to
    ``invoice:status`` when absent.
    """
    return event_key in (payment.processed_events or [])


def remember_tonpays_event(payment: TonPaysPayment, event_key: str) -> None:
    """Marks a webhook delivery as handled.

    The list is rebuilt rather than mutated in place: SQLAlchemy tracks JSON
    column changes by assignment, otherwise the UPDATE would be skipped.
    """
    processed = list(payment.processed_events or [])
    if event_key not in processed:
        processed.append(event_key)
    payment.processed_events = processed


async def get_pending_tonpays_payments(db: AsyncSession, user_id: int) -> list[TonPaysPayment]:
    """Returns the user's unfinished payments."""
    result = await db.execute(
        select(TonPaysPayment).where(
            TonPaysPayment.user_id == user_id,
            TonPaysPayment.status.in_(['pending', 'processing']),
            TonPaysPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def link_tonpays_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: TonPaysPayment,
    transaction_id: int,
) -> TonPaysPayment:
    """Links a payment to a transaction."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
