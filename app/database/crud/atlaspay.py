"""CRUD operations for AtlasPay payments (api.atlaspay.space)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AtlasPayPayment


logger = structlog.get_logger(__name__)


async def create_atlaspay_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'IRT',
    description: str | None = None,
    atlaspay_payment_id: str | None = None,
    tracking_code: str | None = None,
    total_amount_kopeks: int | None = None,
    payment_url: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> AtlasPayPayment:
    """Creates an AtlasPay payment record."""
    payment = AtlasPayPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        total_amount_kopeks=total_amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        atlaspay_payment_id=atlaspay_payment_id,
        tracking_code=tracking_code,
        expires_at=expires_at,
        metadata_json=metadata_json,
        processed_events=[],
        status='pending',
        is_paid=False,
        requires_manual_delivery=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('AtlasPay payment created', order_id=order_id, user_id=user_id)
    return payment


async def get_atlaspay_payment_by_order_id(db: AsyncSession, order_id: str) -> AtlasPayPayment | None:
    """Gets a payment by our merchantOrderRef."""
    result = await db.execute(select(AtlasPayPayment).where(AtlasPayPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_atlaspay_payment_by_provider_id(db: AsyncSession, atlaspay_payment_id: str) -> AtlasPayPayment | None:
    """Gets a payment by the AtlasPay orderId."""
    result = await db.execute(
        select(AtlasPayPayment).where(AtlasPayPayment.atlaspay_payment_id == atlaspay_payment_id)
    )
    return result.scalar_one_or_none()


async def get_atlaspay_payment_by_id(db: AsyncSession, payment_id: int) -> AtlasPayPayment | None:
    """Gets a payment by local ID."""
    result = await db.execute(select(AtlasPayPayment).where(AtlasPayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_atlaspay_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> AtlasPayPayment | None:
    """Gets a payment with a FOR UPDATE lock."""
    result = await db.execute(
        select(AtlasPayPayment)
        .where(AtlasPayPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_atlaspay_payment_status(
    db: AsyncSession,
    payment: AtlasPayPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    atlaspay_payment_id: str | None = None,
    tracking_code: str | None = None,
    total_amount_kopeks: int | None = None,
    received_amount_kopeks: int | None = None,
    requires_manual_delivery: bool | None = None,
    payment_url: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> AtlasPayPayment:
    """Updates a payment's status."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if atlaspay_payment_id is not None:
        payment.atlaspay_payment_id = atlaspay_payment_id
    if tracking_code is not None:
        payment.tracking_code = tracking_code
    if total_amount_kopeks is not None:
        payment.total_amount_kopeks = total_amount_kopeks
    if received_amount_kopeks is not None:
        payment.received_amount_kopeks = received_amount_kopeks
    if requires_manual_delivery is not None:
        payment.requires_manual_delivery = requires_manual_delivery
    if payment_url is not None:
        payment.payment_url = payment_url
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info('AtlasPay payment status updated', order_id=payment.order_id, status=status, is_paid=payment.is_paid)
    return payment


def is_atlaspay_event_processed(payment: AtlasPayPayment, event_key: str) -> bool:
    """Whether a webhook event was already handled.

    The provider sends no delivery id and no retries, so idempotency is keyed
    on ``merchantOrderRef:event`` as a safety net.
    """
    return event_key in (payment.processed_events or [])


def remember_atlaspay_event(payment: AtlasPayPayment, event_key: str) -> None:
    """Marks a webhook event as handled.

    The list is rebuilt rather than mutated in place: SQLAlchemy tracks JSON
    column changes by assignment, otherwise the UPDATE would be skipped.
    """
    processed = list(payment.processed_events or [])
    if event_key not in processed:
        processed.append(event_key)
    payment.processed_events = processed


async def get_pending_atlaspay_payments(db: AsyncSession, user_id: int) -> list[AtlasPayPayment]:
    """Returns the user's unfinished payments."""
    result = await db.execute(
        select(AtlasPayPayment).where(
            AtlasPayPayment.user_id == user_id,
            AtlasPayPayment.status.in_(['pending', 'processing', 'underpaid_review']),
            AtlasPayPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def link_atlaspay_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: AtlasPayPayment,
    transaction_id: int,
) -> AtlasPayPayment:
    """Links a payment to a transaction."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
