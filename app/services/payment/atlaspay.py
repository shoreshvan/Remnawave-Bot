"""Mixin for AtlasPay integration (api.atlaspay.space, card-to-card + SMS-verify)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.atlaspay_service import atlaspay_service, toman_to_kopeks
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# AtlasPay order status -> internal status. Keys are lowercased.
# ``confirmed``/``settled`` additionally require paid=True AND
# requiresManualDelivery=False (accepted-underpayment trap, docs §4.1).
ATLASPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'awaiting_payment': ('pending', False),
    'admin_review': ('processing', False),
    'underpaid_review': ('underpaid_review', False),
    'underpaid_awaiting_remainder': ('processing', False),
    'confirmed': ('success', True),
    'settled': ('success', True),
    'rejected': ('rejected', False),
    'expired': ('expired', False),
    'cancelled': ('cancelled', False),
}

# Statuses the payment can no longer leave.
ATLASPAY_FINAL_STATUSES = frozenset({'amount_mismatch'})

# Statuses from which the payment can still become paid (background reconciliation).
ATLASPAY_PENDING_STATUSES = frozenset({'pending', 'processing', 'underpaid_review'})

# Provider success statuses (still gated on paid + not requiresManualDelivery).
ATLASPAY_SUCCESS_STATUSES = frozenset({'confirmed', 'settled'})


def _parse_provider_dt(value: Any) -> datetime | None:
    """Parses AtlasPay ISO timestamps (paymentDeadlineAt, webhook timestamp)."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    try:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception:
        return None


class AtlaspayPaymentMixin:
    """Mixin for working with AtlasPay payments."""

    async def create_atlaspay_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        customer_telegram_id: int | None = None,
        description: str = 'Balance top-up',
        language: str = 'ru',
    ) -> dict[str, Any] | None:
        """Creates an AtlasPay order and returns the mini-app start link."""
        if not settings.is_atlaspay_enabled():
            logger.error('AtlasPay is not configured')
            return None

        min_amount = settings.ATLASPAY_MIN_AMOUNT_KOPEKS
        max_amount = settings.ATLASPAY_MAX_AMOUNT_KOPEKS

        if amount_kopeks < min_amount:
            logger.warning('AtlasPay: amount below minimum', amount_kopeks=amount_kopeks, min_kopeks=min_amount)
            return None
        if amount_kopeks > max_amount:
            logger.warning('AtlasPay: amount above maximum', amount_kopeks=amount_kopeks, max_kopeks=max_amount)
            return None
        if amount_kopeks % 100 != 0:
            # Provider speaks whole Toman; truncating here would later trip the
            # strict total check (amount_mismatch). Fail fast.
            logger.warning('AtlasPay: amount is not a whole Toman', amount_kopeks=amount_kopeks)
            return None

        payment_module = import_module('app.services.payment_service')
        telegram_id: int | None = customer_telegram_id
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            if user and getattr(user, 'telegram_id', None):
                telegram_id = int(user.telegram_id)

        # customerTelegramId is optional — guest/email flows create unbound orders.
        tg_part = telegram_id if telegram_id else 'guest'
        order_ref = f'ap{tg_part}_{uuid.uuid4().hex[:8]}'

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            api_result = await atlaspay_service.create_order(
                merchant_order_ref=order_ref,
                amount_kopeks=amount_kopeks,
                customer_telegram_id=telegram_id,
                webhook_url=settings.get_atlaspay_callback_url(),
            )

            provider_id = api_result.get('orderId')
            total_kopeks = toman_to_kopeks(api_result.get('totalAmountToman'))
            payment_url = api_result.get('customerStartLink')
            expires_at = _parse_provider_dt(api_result.get('paymentDeadlineAt')) or (
                datetime.now(UTC) + timedelta(minutes=settings.ATLASPAY_DEADLINE_MINUTES)
            )

            atlaspay_crud = import_module('app.database.crud.atlaspay')
            local_payment = await atlaspay_crud.create_atlaspay_payment(
                db=db,
                user_id=user_id,
                order_id=order_ref,
                amount_kopeks=amount_kopeks,
                currency='IRT',
                description=description,
                atlaspay_payment_id=str(provider_id) if provider_id is not None else None,
                tracking_code=api_result.get('trackingCode'),
                total_amount_kopeks=total_kopeks,
                payment_url=payment_url,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info('AtlasPay: payment created', order_id=order_ref, user_id=user_id, amount_kopeks=amount_kopeks)

            return {
                'order_id': order_ref,
                'provider_order_id': provider_id,
                'tracking_code': api_result.get('trackingCode'),
                'amount_kopeks': amount_kopeks,
                'total_amount_kopeks': total_kopeks,
                'currency': 'IRT',
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('AtlasPay: payment creation error', error=e)
            return None

    async def process_atlaspay_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Handles an AtlasPay webhook (signature already verified in webserver).

        The webhook body carries NO ``paid`` / ``requiresManualDelivery``
        fields, so every delivery is followed by a ``verify`` call and the
        decision is made on fresh provider data. Idempotent on
        ``merchantOrderRef:event``.
        """
        try:
            our_order_ref = payload.get('merchantOrderRef')
            provider_id = payload.get('orderId')
            event = (payload.get('event') or '').strip().lower()
            raw_status = (payload.get('status') or '').strip().lower()

            if not our_order_ref:
                logger.warning('AtlasPay callback: missing merchantOrderRef', payload=payload)
                return False

            atlaspay_crud = import_module('app.database.crud.atlaspay')
            payment = await atlaspay_crud.get_atlaspay_payment_by_order_id(db, our_order_ref)
            if not payment and provider_id is not None:
                payment = await atlaspay_crud.get_atlaspay_payment_by_provider_id(db, str(provider_id))
            if not payment:
                # A foreign ref will not appear on retry — ack the delivery.
                logger.warning('AtlasPay callback: payment not found', order_id=our_order_ref)
                return True

            event_key = f'{payment.order_id}:{event or raw_status}'
            if atlaspay_crud.is_atlaspay_event_processed(payment, event_key):
                logger.info('AtlasPay callback: event already processed', order_id=our_order_ref, event_key=event_key)
                return True

            locked = await atlaspay_crud.get_atlaspay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('AtlasPay: failed to lock payment', payment_id=payment.id)
                return False
            payment = locked

            # Re-check under the lock: a parallel delivery of the same event could
            # have slipped between the read and the row lock.
            if atlaspay_crud.is_atlaspay_event_processed(payment, event_key):
                logger.info('AtlasPay callback: event already processed (locked)', event_key=event_key)
                return True

            if payment.status in ATLASPAY_FINAL_STATUSES:
                logger.warning(
                    'AtlasPay callback: payment in final status, event ignored',
                    order_id=payment.order_id,
                    current_status=payment.status,
                    incoming_status=raw_status,
                )
                atlaspay_crud.remember_atlaspay_event(payment, event_key)
                await db.commit()
                return True

            if payment.is_paid:
                logger.info('AtlasPay callback: payment already paid', order_id=payment.order_id)
                atlaspay_crud.remember_atlaspay_event(payment, event_key)
                await db.commit()
                return True

            # Authoritative decision on fresh provider data (webhook lacks
            # paid/requiresManualDelivery). Missing provider order id or a 404
            # means "not paid yet" — ack and keep polling.
            if payment.atlaspay_payment_id is None:
                atlaspay_crud.remember_atlaspay_event(payment, event_key)
                await db.commit()
                return True

            try:
                status_data = await atlaspay_service.verify_order(order_id=payment.atlaspay_payment_id)
            except Exception as e:
                logger.error('AtlasPay callback: verify call failed', error=e)
                return False
            if not status_data:
                atlaspay_crud.remember_atlaspay_event(payment, event_key)
                await db.commit()
                return True

            return await self._apply_atlaspay_status(
                db,
                payment=payment,
                status_data=status_data,
                event_key=event_key,
                trigger='webhook',
                webhook_payload={
                    'event': event,
                    'status': raw_status,
                    'totalAmountToman': payload.get('totalAmountToman'),
                    'timestamp': payload.get('timestamp'),
                },
            )

        except Exception as e:
            logger.exception('AtlasPay callback: processing error', error=e)
            return False

    async def _apply_atlaspay_status(
        self,
        db: AsyncSession,
        *,
        payment: Any,
        status_data: dict[str, Any],
        event_key: str,
        trigger: str,
        webhook_payload: dict[str, Any] | None = None,
    ) -> bool:
        """Applies fresh provider status to a locked payment. Row lock already held."""
        atlaspay_crud = import_module('app.database.crud.atlaspay')

        raw_status = (status_data.get('status') or '').strip().lower()
        paid_flag = status_data.get('paid') is True
        requires_manual = status_data.get('requiresManualDelivery') is True

        if raw_status not in ATLASPAY_STATUS_MAP:
            logger.warning('AtlasPay: unknown status', order_id=payment.order_id, status=raw_status)
            atlaspay_crud.remember_atlaspay_event(payment, event_key)
            await db.commit()
            return True

        callback_payload = {
            'status': raw_status,
            'paid': paid_flag,
            'requiresManualDelivery': requires_manual,
            'totalAmountToman': status_data.get('totalAmountToman'),
            'actualReceivedAmountToman': status_data.get('actualReceivedAmountToman'),
            'trackingCode': status_data.get('trackingCode'),
        }
        if webhook_payload:
            callback_payload['webhook'] = webhook_payload

        total_kopeks = toman_to_kopeks(status_data.get('totalAmountToman'))
        received_kopeks = toman_to_kopeks(status_data.get('actualReceivedAmountToman'))

        # Full success: confirmed/settled + paid + NO accepted underpayment.
        if raw_status in ATLASPAY_SUCCESS_STATUSES and paid_flag and not requires_manual:
            expected_total = payment.total_amount_kopeks
            if expected_total is None and total_kopeks is not None:
                # Create response carried no total — adopt the first sighted one
                # instead of false-mismatching (flushed/committed downstream).
                payment.total_amount_kopeks = total_kopeks
                expected_total = total_kopeks
            if total_kopeks is None or total_kopeks != expected_total:
                # Provider total drifted from what we showed the buyer —
                # refuse auto-credit, needs manual review.
                logger.error(
                    'AtlasPay total mismatch',
                    expected_kopeks=payment.total_amount_kopeks,
                    received_kopeks=total_kopeks,
                    order_id=payment.order_id,
                )
                atlaspay_crud.remember_atlaspay_event(payment, event_key)
                await atlaspay_crud.update_atlaspay_payment_status(
                    db=db,
                    payment=payment,
                    status='amount_mismatch',
                    is_paid=False,
                    callback_payload=callback_payload,
                )
                return False
            return await self._apply_atlaspay_success(
                db,
                payment=payment,
                event_key=event_key,
                callback_payload=callback_payload,
                received_kopeks=received_kopeks,
                trigger=trigger,
            )

        # Accepted underpayment: looks paid but merchant got less — NEVER
        # auto-credit. Park for admin decision (kept out of polling).
        if requires_manual or (raw_status in ATLASPAY_SUCCESS_STATUSES and paid_flag):
            logger.error(
                'AtlasPay requires manual delivery (accepted underpayment)',
                order_id=payment.order_id,
                received_kopeks=received_kopeks,
                expected_kopeks=payment.amount_kopeks,
            )
            atlaspay_crud.remember_atlaspay_event(payment, event_key)
            await atlaspay_crud.update_atlaspay_payment_status(
                db=db,
                payment=payment,
                status='manual_review',
                is_paid=False,
                total_amount_kopeks=total_kopeks,
                received_amount_kopeks=received_kopeks,
                requires_manual_delivery=True,
                callback_payload=callback_payload,
            )
            return True

        if payment.is_paid:
            logger.info('AtlasPay: payment already paid, non-paid status ignored', order_id=payment.order_id)
            atlaspay_crud.remember_atlaspay_event(payment, event_key)
            await db.commit()
            return True

        internal_status, _ = ATLASPAY_STATUS_MAP[raw_status]
        atlaspay_crud.remember_atlaspay_event(payment, event_key)
        await atlaspay_crud.update_atlaspay_payment_status(
            db=db,
            payment=payment,
            status=internal_status,
            is_paid=None,
            total_amount_kopeks=total_kopeks,
            received_amount_kopeks=received_kopeks,
            requires_manual_delivery=requires_manual or None,
            callback_payload=callback_payload,
        )
        return True

    async def _apply_atlaspay_success(
        self,
        db: AsyncSession,
        *,
        payment: Any,
        event_key: str,
        callback_payload: dict[str, Any],
        received_kopeks: int | None,
        trigger: str,
    ) -> bool:
        """Credits a fully-paid order. Row lock already held."""
        atlaspay_crud = import_module('app.database.crud.atlaspay')

        if payment.is_paid:
            logger.info('AtlasPay: payment already paid', order_id=payment.order_id)
            atlaspay_crud.remember_atlaspay_event(payment, event_key)
            await db.commit()
            return True

        payment.status = 'success'
        payment.is_paid = True
        payment.paid_at = datetime.now(UTC)
        payment.requires_manual_delivery = False
        if received_kopeks is not None:
            payment.received_amount_kopeks = received_kopeks
        payment.callback_payload = callback_payload
        payment.updated_at = datetime.now(UTC)
        atlaspay_crud.remember_atlaspay_event(payment, event_key)
        await db.flush()

        return await self._finalize_atlaspay_payment(db, payment, trigger=trigger)

    async def _finalize_atlaspay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        trigger: str,
    ) -> bool:
        """Creates the transaction, credits the balance and sends notifications.

        The FOR UPDATE lock is already held by the caller. Credits the
        requested base amount (not the unique-delta total).
        """
        payment_module = import_module('app.services.payment_service')
        atlaspay_crud = import_module('app.database.crud.atlaspay')

        if payment.transaction_id:
            logger.info(
                'AtlasPay payment already linked to a transaction',
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
            provider_name='atlaspay',
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
            logger.error('User not found for AtlasPay', user_id=payment.user_id)
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
                PaymentMethod.ATLASPAY,
            )

        display_name = settings.get_atlaspay_display_name()
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
                payment_method=PaymentMethod.ATLASPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await atlaspay_crud.link_atlaspay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('AtlasPay payment already credited the balance earlier', order_id=payment.order_id)
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
            payment_method=PaymentMethod.ATLASPAY,
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
            logger.error('AtlasPay referral top-up processing error', error=error)

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
                logger.error('AtlasPay admin notification error', error=error)

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
                logger.error('AtlasPay user notification error', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('AtlasPay saved-cart handling error', user_id=payment.user_id, error=error, exc_info=True)

        metadata['balance_change'] = {
            'old_balance': old_balance,
            'new_balance': user.balance_kopeks,
            'credited_at': datetime.now(UTC).isoformat(),
        }
        metadata['balance_credited'] = True
        payment.metadata_json = metadata
        await db.commit()

        logger.info('AtlasPay payment processed', order_id=payment.order_id, user_id=payment.user_id, trigger=trigger)

        return True

    async def check_atlaspay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Checks an order via verify (authoritative paid flag) and syncs the DB.

        Used by the «paid» button, the admin manual check and the background
        reconciliation.
        """
        try:
            atlaspay_crud = import_module('app.database.crud.atlaspay')
            payment = await atlaspay_crud.get_atlaspay_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('AtlasPay payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {'payment': payment, 'status': payment.status, 'is_paid': True}

            if payment.status in ATLASPAY_FINAL_STATUSES:
                return {'payment': payment, 'status': payment.status, 'is_paid': False}

            if payment.atlaspay_payment_id is None:
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            try:
                status_data = await atlaspay_service.verify_order(order_id=payment.atlaspay_payment_id)
            except Exception as e:
                logger.error('Error checking AtlasPay order via API', error=e)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

            if not status_data:
                logger.warning('AtlasPay API check: order not found at provider', order_id=payment.order_id)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            locked = await atlaspay_crud.get_atlaspay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('AtlasPay: failed to lock payment', payment_id=payment.id)
                return None
            payment = locked

            if payment.is_paid:
                logger.info('AtlasPay payment already processed (api_check)', order_id=payment.order_id)
                return {'payment': payment, 'status': 'success', 'is_paid': True}

            event_key = f'{payment.order_id}:api_check:{(status_data.get("status") or "").strip().lower()}'
            if atlaspay_crud.is_atlaspay_event_processed(payment, event_key):
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

            logger.info('AtlasPay order re-checked via API', order_id=payment.order_id)

            await self._apply_atlaspay_status(
                db,
                payment=payment,
                status_data=status_data,
                event_key=event_key,
                trigger='api_check',
            )

            return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

        except Exception as e:
            logger.exception('AtlasPay: status check error', error=e)
            return None
