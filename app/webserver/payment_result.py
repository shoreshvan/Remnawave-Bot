import hmac

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.nowpayments import get_nowpayments_payment_by_order_id
from app.database.database import get_db
from app.utils.payment_result_token import payment_result_token


def payment_result_data(payment):
    metadata = payment.metadata_json or {}
    credited = bool(payment.is_paid and payment.transaction_id)
    state = 'success' if credited else payment.status
    if state == 'success' and not credited:
        state = 'processing'
    return {
        'status': state,
        'credited': credited,
        'invoice': payment.invoice_id or payment.order_id,
        'transaction': str(payment.transaction_id) if credited else None,
        'amount_toman': str(payment.amount_kopeks // 100),
        'created_at': payment.created_at.isoformat() if payment.created_at else None,
        'paid_at': payment.paid_at.isoformat() if credited and payment.paid_at else None,
        'pay_currency': payment.pay_currency,
        'actually_paid': str(payment.actually_paid) if payment.actually_paid is not None else None,
        'manual': bool(metadata.get('manual_credit') or f'{payment.order_id}:force_credit' in (payment.processed_events or [])),
        'back': f'https://t.me/{settings.BOT_USERNAME.lstrip("@")}' if settings.BOT_USERNAME else None,
        'support': settings.get_support_contact_url(),
    }


def create_payment_result_router():
    router = APIRouter()

    @router.get(settings.NOWPAYMENTS_RESULT_PATH.rstrip('/') + '/status')
    async def result_status(
        invoice: str = Query(min_length=1, max_length=128),
        token: str = Query(min_length=32, max_length=32),
        db: AsyncSession = Depends(get_db),
    ):
        if not hmac.compare_digest(token, payment_result_token(invoice)):
            raise HTTPException(status_code=404, detail='Payment not found')
        payment = await get_nowpayments_payment_by_order_id(db, invoice)
        if not payment:
            raise HTTPException(status_code=404, detail='Payment not found')
        return JSONResponse(payment_result_data(payment), headers={'Cache-Control': 'no-store'})

    return router
