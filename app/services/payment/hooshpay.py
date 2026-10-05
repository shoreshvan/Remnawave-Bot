"""Mixin for HooshPay integration (hooshpay.xyz/api/v1, card-to-card)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.hooshpay_service import hooshpay_service, toman_to_kopeks
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# HooshPay invoice status -> (internal status, is_paid). Keys are lowercased.
HOOSHPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'pending': ('pending', False),
    'paid': ('success', True),
    'expired': ('expired', False),
    'cancelled': ('cancelled', False),
    'failed': ('failed', False),
}

# Statuses the payment can no longer leave.
HOOSHPAY_FINAL_STATUSES = frozenset({'amount_mismatch'})

# Statuses from which the payment can still become paid (background reconciliation).
HOOSHPAY_PENDING_STATUSES = frozenset({'pending'})


class HooshpayPaymentMixin:
    """Mixin for working with HooshPay payments."""

    async def create_hooshpay_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Balance top-up',
        language: str = 'ru',
        return_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Creates a HooshPay invoice and returns data for the hosted pay page."""
        if not settings.is_hooshpay_enabled():
            logger.error('HooshPay is not configured')
            return None

        min_amount = settings.HOOSHPAY_MIN_AMOUNT_KOPEKS
        max_amount = settings.HOOSHPAY_MAX_AMOUNT_KOPEKS

        if amount_kopeks < min_amount:
            logger.warning('HooshPay: amount below minimum', amount_kopeks=amount_kopeks, min_kopeks=min_amount)
            return None
        if amount_kopeks > max_amount:
            logger.warning('HooshPay: amount above maximum', amount_kopeks=amount_kopeks, max_kopeks=max_amount)
            return None

        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            tg_id = None

        order_id = f'hp{tg_id or "guest"}_{uuid.uuid4().hex[:8]}'
        lifetime = settings.HOOSHPAY_INVOICE_LIFETIME_MINUTES

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            api_result = await hooshpay_service.create_invoice(
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                description=(description[:255] if description else None),
                fee_mode=(settings.HOOSHPAY_FEE_MODE or None),
                callback_url=settings.get_hooshpay_callback_url(),
                return_url=return_url or settings.get_hooshpay_return_url(),
            )

            uid = api_result.get('uid')
            payment_url = api_result.get('payment_url')
            payable_amount_kopeks = toman_to_kopeks(api_result.get('payable_amount'))
            # The provider returns "Y-m-d H:i:s" without a timezone; computing the
            # deadline ourselves from the lifetime is safer than guessing the zone.
            expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)

            hooshpay_crud = import_module('app.database.crud.hooshpay')
            local_payment = await hooshpay_crud.create_hooshpay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency='IRT',
                description=description,
                payment_url=payment_url,
                hooshpay_payment_id=str(uid) if uid else None,
                payable_amount_kopeks=payable_amount_kopeks,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info('HooshPay: payment created', order_id=order_id, user_id=user_id, amount_kopeks=amount_kopeks)

            return {
                'order_id': order_id,
                'uid': str(uid) if uid else None,
                'amount_kopeks': amount_kopeks,
                'payable_amount_kopeks': payable_amount_kopeks,
                'currency': 'IRT',
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('HooshPay: payment creation error', error=e)
            return None

    async def process_hooshpay_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Handles a HooshPay webhook (signature already verified in webserver).

        Idempotent on (invoice uid, status): a redelivered event never credits
        the balance twice.
        """
        try:
            our_order_id = payload.get('order_id')
            hooshpay_uid = payload.get('invoice')
            raw_status = (payload.get('status') or '').strip().lower()

            if not our_order_id or not raw_status or not hooshpay_uid:
                logger.warning('HooshPay callback: missing required fields', payload=payload)
                return False

            hooshpay_crud = import_module('app.database.crud.hooshpay')
            payment = await hooshpay_crud.get_hooshpay_payment_by_order_id(db, our_order_id)
            if not payment:
                # A foreign order_id will not appear on retry — ack the delivery.
                logger.warning('HooshPay callback: payment not found', order_id=our_order_id)
                return True

            event_key = f'{hooshpay_uid}:{raw_status}'
            if hooshpay_crud.is_hooshpay_event_processed(payment, event_key):
                logger.info('HooshPay callback: event already processed', order_id=our_order_id, event_key=event_key)
                return True

            locked = await hooshpay_crud.get_hooshpay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('HooshPay: failed to lock payment', payment_id=payment.id)
                return False
            payment = locked

            # Re-check under the lock: a parallel delivery of the same event could
            # have slipped between the read and the row lock.
            if hooshpay_crud.is_hooshpay_event_processed(payment, event_key):
                logger.info('HooshPay callback: event already processed (locked)', event_key=event_key)
                return True

            if raw_status not in HOOSHPAY_STATUS_MAP:
                logger.warning('HooshPay callback: unknown status', order_id=payment.order_id, status=raw_status)
                return True

            if payment.status in HOOSHPAY_FINAL_STATUSES:
                logger.warning(
                    'HooshPay callback: payment in final status, event ignored',
                    order_id=payment.order_id,
                    current_status=payment.status,
                    incoming_status=raw_status,
                )
                hooshpay_crud.remember_hooshpay_event(payment, event_key)
                await db.commit()
                return True

            internal_status, is_paid = HOOSHPAY_STATUS_MAP[raw_status]

            callback_payload = {
                'invoice': hooshpay_uid,
                'status': raw_status,
                'amount': payload.get('amount'),
                'payable_amount': payload.get('payable_amount'),
                'merchant_credit': payload.get('merchant_credit'),
                'fee_amount': payload.get('fee_amount'),
                'fee_mode': payload.get('fee_mode'),
                'tracking_code': payload.get('tracking_code'),
            }

            if is_paid:
                return await self._apply_hooshpay_success(
                    db,
                    payment=payment,
                    payload=payload,
                    event_key=event_key,
                    hooshpay_uid=str(hooshpay_uid),
                    callback_payload=callback_payload,
                )

            hooshpay_crud.remember_hooshpay_event(payment, event_key)
            await hooshpay_crud.update_hooshpay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=None,
                hooshpay_payment_id=str(hooshpay_uid),
                callback_payload=callback_payload,
            )
            return True

        except Exception as e:
            logger.exception('HooshPay callback: processing error', error=e)
            return False

    async def _apply_hooshpay_success(
        self,
        db: AsyncSession,
        *,
        payment: Any,
        payload: dict[str, Any],
        event_key: str,
        hooshpay_uid: str,
        callback_payload: dict[str, Any],
    ) -> bool:
        """Verifies the amount and credits the payment. Row lock already held."""
        hooshpay_crud = import_module('app.database.crud.hooshpay')

        received_kopeks = toman_to_kopeks(payload.get('amount'))
        if received_kopeks is None:
            logger.error(
                'HooshPay callback: PAID without a parsable amount, crediting cancelled',
                order_id=payment.order_id,
                received=payload.get('amount'),
            )
            return False

        if received_kopeks != payment.amount_kopeks:
            logger.error(
                'HooshPay amount mismatch',
                expected_kopeks=payment.amount_kopeks,
                received_kopeks=received_kopeks,
                order_id=payment.order_id,
            )
            hooshpay_crud.remember_hooshpay_event(payment, event_key)
            await hooshpay_crud.update_hooshpay_payment_status(
                db=db,
                payment=payment,
                status='amount_mismatch',
                is_paid=False,
                callback_payload=callback_payload,
            )
            return False

        if payment.is_paid:
            logger.info('HooshPay callback: payment already paid', order_id=payment.order_id)
            hooshpay_crud.remember_hooshpay_event(payment, event_key)
            await db.commit()
            return True

        payment.status = 'success'
        payment.is_paid = True
        payment.paid_at = datetime.now(UTC)
        payment.hooshpay_payment_id = hooshpay_uid or payment.hooshpay_payment_id
        # payable_amount is what the buyer paid; merchant_credit is the net we
        # receive. We credit the full requested invoice amount to the user and
        # keep both figures only for reconciliation with the HooshPay panel.
        payable = toman_to_kopeks(payload.get('payable_amount'))
        if payable is not None:
            payment.payable_amount_kopeks = payable
        credited = toman_to_kopeks(payload.get('merchant_credit'))
        if credited is not None:
            payment.credited_kopeks = credited
        payment.callback_payload = callback_payload
        payment.updated_at = datetime.now(UTC)
        hooshpay_crud.remember_hooshpay_event(payment, event_key)
        await db.flush()

        return await self._finalize_hooshpay_payment(db, payment, trigger='webhook')

    async def _finalize_hooshpay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        trigger: str,
    ) -> bool:
        """Creates the transaction, credits the balance and sends notifications.

        The FOR UPDATE lock is already held by the caller.
        """
        payment_module = import_module('app.services.payment_service')
        hooshpay_crud = import_module('app.database.crud.hooshpay')

        if payment.transaction_id:
            logger.info(
                'HooshPay payment already linked to a transaction',
                order_id=payment.order_id,
                transaction_id=payment.transaction_id,
                trigger=trigger,
            )
            return True

        metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=payment.order_id,
            provider_name='hooshpay',
        )
        if guest_result is not None:
            return True

        if not payment.is_paid:
            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.updated_at = datetime.now(UTC)

        balance_already_credited = bool(metadata.get('balance_credited'))

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('User not found for HooshPay', user_id=payment.user_id)
            return False

        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = payment.order_id

        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.HOOSHPAY,
            )

        display_name = settings.get_hooshpay_display_name()
        description = f'Пополнение через {display_name}'

        transaction = existing_transaction
        created_transaction = False

        if not transaction:
            transaction = await payment_module.create_transaction(
                db,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=payment.amount_kopeks,
                description=description,
                payment_method=PaymentMethod.HOOSHPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await hooshpay_crud.link_hooshpay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('HooshPay payment already credited the balance earlier', order_id=payment.order_id)
            return True

        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(user)

        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.HOOSHPAY,
            external_id=transaction_external_id,
        )

        topup_status = '\U0001f195 Первое пополнение' if was_first_topup else '\U0001f504 Пополнение'

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                payment.amount_kopeks,
                getattr(self, 'bot', None),
            )
        except Exception as error:
            logger.error('HooshPay referral top-up processing error', error=error)

        if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
            user.has_made_first_topup = True
            await db.commit()
            await db.refresh(user)

        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    db=db,
                )
            except Exception as error:
                logger.error('HooshPay admin notification error', error=error)

        if getattr(self, 'bot', None) and user.telegram_id and settings.is_notifications_enabled():
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'\U0001f4b0 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'\U0001f4b3 Способ: {display_name}\n'
                        f'\U0001f194 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('HooshPay user notification error', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('HooshPay saved-cart handling error', user_id=payment.user_id, error=error, exc_info=True)

        metadata['balance_change'] = {
            'old_balance': old_balance,
            'new_balance': user.balance_kopeks,
            'credited_at': datetime.now(UTC).isoformat(),
        }
        metadata['balance_credited'] = True
        payment.metadata_json = metadata
        await db.commit()

        logger.info('HooshPay payment processed', order_id=payment.order_id, user_id=payment.user_id, trigger=trigger)

        return True

    async def check_hooshpay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Checks a payment via the HooshPay API and syncs the DB.

        A safety net for a lost webhook: used by the admin manual check and the
        background reconciliation.
        """
        try:
            hooshpay_crud = import_module('app.database.crud.hooshpay')
            payment = await hooshpay_crud.get_hooshpay_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('HooshPay payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {'payment': payment, 'status': payment.status, 'is_paid': True}

            if payment.status in HOOSHPAY_FINAL_STATUSES:
                return {'payment': payment, 'status': payment.status, 'is_paid': False}

            if not payment.hooshpay_payment_id:
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            try:
                status_data = await hooshpay_service.get_invoice(uid=payment.hooshpay_payment_id)
            except Exception as e:
                logger.error('Error checking HooshPay payment status via API', error=e)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

            if not status_data:
                logger.warning('HooshPay API check: invoice not found at provider', order_id=payment.order_id)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            raw_status = (status_data.get('status') or '').strip().lower()
            internal_status, is_paid = HOOSHPAY_STATUS_MAP.get(raw_status, ('pending', False))

            if not is_paid:
                if internal_status != payment.status:
                    payment = await hooshpay_crud.update_hooshpay_payment_status(
                        db=db,
                        payment=payment,
                        status=internal_status,
                    )
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            received_kopeks = toman_to_kopeks(status_data.get('amount'))
            if received_kopeks is None or received_kopeks != payment.amount_kopeks:
                logger.error(
                    'HooshPay amount mismatch (API check)',
                    expected_kopeks=payment.amount_kopeks,
                    received_kopeks=received_kopeks,
                    order_id=payment.order_id,
                )
                await hooshpay_crud.update_hooshpay_payment_status(
                    db=db,
                    payment=payment,
                    status='amount_mismatch',
                    is_paid=False,
                    callback_payload={'check_source': 'api', 'hooshpay_status_data': status_data},
                )
                return {'payment': payment, 'status': 'amount_mismatch', 'is_paid': False}

            locked = await hooshpay_crud.get_hooshpay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('HooshPay: failed to lock payment', payment_id=payment.id)
                return None
            payment = locked

            if payment.is_paid:
                logger.info('HooshPay payment already processed (api_check)', order_id=payment.order_id)
                return {'payment': payment, 'status': 'success', 'is_paid': True}

            logger.info('HooshPay payment confirmed via API', order_id=payment.order_id)

            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.callback_payload = {'check_source': 'api', 'hooshpay_status_data': status_data}
            payment.updated_at = datetime.now(UTC)
            hooshpay_crud.remember_hooshpay_event(payment, f'{payment.hooshpay_payment_id}:{raw_status}')
            await db.flush()

            await self._finalize_hooshpay_payment(db, payment, trigger='api_check')

            return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

        except Exception as e:
            logger.exception('HooshPay: status check error', error=e)
            return None
