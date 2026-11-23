from datetime import UTC, datetime
from importlib import import_module

from app.config import settings
from app.database.models import PaymentMethod


IRANIAN_FORCE_CREDIT_METHODS = frozenset({
    PaymentMethod.ATLASPAY, PaymentMethod.HOOSHPAY, PaymentMethod.TONPAYS,
})
FORCE_CREDIT_STATUSES = frozenset({
    'pending', 'processing', 'need_action', 'underpaid_review', 'manual_review', 'amount_mismatch',
})


def can_force_credit(method, payment):
    return (
        method in IRANIAN_FORCE_CREDIT_METHODS
        and not payment.is_paid
        and not payment.transaction_id
        and payment.user_id is not None
        and payment.amount_kopeks > 0
        and (payment.status or '').lower() in FORCE_CREDIT_STATUSES
    )


async def force_credit_iranian_payment(db, payment_service, method, payment_id, *, admin_id):
    if method not in IRANIAN_FORCE_CREDIT_METHODS or not settings.is_admin(admin_id):
        return None
    provider = method.value
    crud = import_module(f'app.database.crud.{provider}')
    payment = await getattr(crud, f'get_{provider}_payment_by_id_for_update')(db, payment_id)
    if not payment:
        return None
    if payment.is_paid:
        return payment
    if not can_force_credit(method, payment):
        return None
    metadata = dict(payment.metadata_json or {})
    metadata['manual_credit'] = {
        'admin_telegram_id': admin_id,
        'credited_at': datetime.now(UTC).isoformat(),
        'previous_status': payment.status,
        'amount_kopeks': payment.amount_kopeks,
        'provider_verified': False,
    }
    payment.metadata_json = metadata
    getattr(crud, f'remember_{provider}_event')(payment, f'{payment.order_id}:force_credit')
    payment.status = 'success'
    payment.is_paid = True
    payment.paid_at = datetime.now(UTC)
    payment.updated_at = payment.paid_at
    try:
        await db.flush()
        ok = await getattr(payment_service, f'_finalize_{provider}_payment')(db, payment, trigger='admin_force')
        if not ok:
            await db.rollback()
            return None
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return payment
