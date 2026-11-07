"""Mixin for TonPays custom gateway integration (tonpays.online, Telegram platform)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.tonpays_service import tonpays_service, toman_to_kopeks
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# TonPays invoice status -> (internal status, is_paid). Keys are lowercased.
# ``completed`` additionally requires the ``paid`` flag (checked by the caller).
TONPAYS_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'pending': ('pending', False),
    'processing': ('processing', False),
    'need_action': ('need_action', False),
    'completed': ('success', True),
    'rejected': ('rejected', False),
    'expired': ('expired', False),
    'canceled': ('cancelled', False),
}

# Statuses the payment can no longer leave.
TONPAYS_FINAL_STATUSES = frozenset({'amount_mismatch'})

# Statuses from which the payment can still become paid (background reconciliation).
TONPAYS_PENDING_STATUSES = frozenset({'pending', 'processing', 'need_action'})


def _parse_provider_ts(value: Any) -> datetime | None:
    """Parses TonPays ``occurred_at`` (unix timestamp) into aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(str(value).strip()), tz=UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _delivery_event_key(payload: dict[str, Any]) -> str:
    """Idempotency key: provider ``delivery_id`` (invoice:status:timestamp)."""
    delivery_id = payload.get('delivery_id')
    if delivery_id:
        return str(delivery_id)
    return f"{payload.get('invoice_id')}:{str(payload.get('status') or '').strip().lower()}"


class TonpaysPaymentMixin:
    """Mixin for working with TonPays payments."""

    async def create_tonpays_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        buyer_chat_id: int | None = None,
        description: str = 'Balance top-up',
        language: str = 'ru',
    ) -> dict[str, Any] | None:
        """Creates a TonPays invoice and returns card details for manual transfer."""
        if not settings.is_tonpays_enabled():
            logger.error('TonPays is not configured')
            return None

        min_amount = settings.TONPAYS_MIN_AMOUNT_KOPEKS
        max_amount = settings.TONPAYS_MAX_AMOUNT_KOPEKS

        if amount_kopeks < min_amount:
            logger.warning('TonPays: amount below minimum', amount_kopeks=amount_kopeks, min_kopeks=min_amount)
            return None
        if amount_kopeks > max_amount:
            logger.warning('TonPays: amount above maximum', amount_kopeks=amount_kopeks, max_kopeks=max_amount)
            return None
        if amount_kopeks % 100 != 0:
            # Provider speaks whole Toman; truncating here would later trip the
            # strict amount check in the webhook (amount_mismatch). Fail fast.
            logger.warning('TonPays: amount is not a whole Toman', amount_kopeks=amount_kopeks)
            return None

        payment_module = import_module('app.services.payment_service')
        telegram_id: int | None = buyer_chat_id
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            if user and getattr(user, 'telegram_id', None):
                telegram_id = int(user.telegram_id)
        if not telegram_id:
            # The Telegram platform mandates buyer_chat_id — guest/email-only
            # flows cannot use this gateway.
            logger.warning('TonPays: buyer_chat_id is required', user_id=user_id)
            return None

        # Provider caps order_id at 20 chars.
        order_id = f'tp{uuid.uuid4().hex[:12]}'
        lifetime = settings.TONPAYS_INVOICE_LIFETIME_MINUTES

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            api_result = await tonpays_service.create_invoice(
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                buyer_chat_id=telegram_id,
                callback_url=settings.get_tonpays_callback_url(),
            )

            invoice_id = api_result.get('invoice_id')
            final_amount_kopeks = toman_to_kopeks(api_result.get('final_amount'))
            expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)

            tonpays_crud = import_module('app.database.crud.tonpays')
            local_payment = await tonpays_crud.create_tonpays_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency='IRT',
                description=description,
                tonpays_payment_id=str(invoice_id) if invoice_id else None,
                final_amount_kopeks=final_amount_kopeks,
                card_number=api_result.get('card_number'),
                card_name=api_result.get('card_name'),
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info('TonPays: payment created', order_id=order_id, user_id=user_id, amount_kopeks=amount_kopeks)

            return {
                'order_id': order_id,
                'invoice_id': str(invoice_id) if invoice_id else None,
                'amount_kopeks': amount_kopeks,
                'final_amount_kopeks': final_amount_kopeks,
                'currency': 'IRT',
                'card_number': api_result.get('card_number'),
                'card_name': api_result.get('card_name'),
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('TonPays: payment creation error', error=e)
            return None

    async def process_tonpays_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Handles a TonPays webhook (auth already verified in webserver).

        Idempotent on the provider ``delivery_id``: a redelivered event never
        credits the balance twice.
        """
        try:
            our_order_id = payload.get('order_id')
            invoice_id = payload.get('invoice_id')
            raw_status = (payload.get('status') or '').strip().lower()
            paid_flag = payload.get('paid') is True

            if not our_order_id or not raw_status or not invoice_id:
                logger.warning('TonPays callback: missing required fields', payload=payload)
                return False

            tonpays_crud = import_module('app.database.crud.tonpays')
            payment = await tonpays_crud.get_tonpays_payment_by_order_id(db, our_order_id)
            if not payment and invoice_id:
                payment = await tonpays_crud.get_tonpays_payment_by_invoice_id(db, str(invoice_id))
            if not payment:
                # A foreign order_id will not appear on retry — ack the delivery.
                logger.warning('TonPays callback: payment not found', order_id=our_order_id)
                return True

            event_key = _delivery_event_key(payload)
            if tonpays_crud.is_tonpays_event_processed(payment, event_key):
                logger.info('TonPays callback: event already processed', order_id=our_order_id, event_key=event_key)
                return True

            locked = await tonpays_crud.get_tonpays_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('TonPays: failed to lock payment', payment_id=payment.id)
                return False
            payment = locked

            # Re-check under the lock: a parallel delivery of the same event could
            # have slipped between the read and the row lock.
            if tonpays_crud.is_tonpays_event_processed(payment, event_key):
                logger.info('TonPays callback: event already processed (locked)', event_key=event_key)
                return True

            if raw_status not in TONPAYS_STATUS_MAP:
                logger.warning('TonPays callback: unknown status', order_id=payment.order_id, status=raw_status)
                return True

            if payment.status in TONPAYS_FINAL_STATUSES:
                logger.warning(
                    'TonPays callback: payment in final status, event ignored',
                    order_id=payment.order_id,
                    current_status=payment.status,
                    incoming_status=raw_status,
                )
                tonpays_crud.remember_tonpays_event(payment, event_key)
                await db.commit()
                return True

            internal_status, _ = TONPAYS_STATUS_MAP[raw_status]
            # ``completed`` alone is not money — the ``paid`` flag decides.
            is_paid = paid_flag and raw_status == 'completed'
            if raw_status == 'completed' and not paid_flag:
                internal_status = 'pending'

            callback_payload = {
                'invoice_id': invoice_id,
                'event': payload.get('event'),
                'delivery_id': payload.get('delivery_id'),
                'status': raw_status,
                'paid': paid_flag,
                'request_amount': payload.get('request_amount'),
                'final_amount': payload.get('final_amount'),
                'credit_amount': payload.get('credit_amount'),
                'occurred_at': payload.get('occurred_at'),
            }

            if is_paid:
                return await self._apply_tonpays_success(
                    db,
                    payment=payment,
                    payload=payload,
                    event_key=event_key,
                    invoice_id=str(invoice_id),
                    callback_payload=callback_payload,
                )

            if payment.is_paid:
                # Late/delayed non-paid delivery must never regress a credited
                # payment. Acknowledge and keep state.
                logger.warning(
                    'TonPays callback: non-paid event for already-paid payment, ignored',
                    order_id=payment.order_id,
                    current_status=payment.status,
                    incoming_status=raw_status,
                )
                tonpays_crud.remember_tonpays_event(payment, event_key)
                await db.commit()
                return True

            tonpays_crud.remember_tonpays_event(payment, event_key)
            await tonpays_crud.update_tonpays_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=None,
                tonpays_payment_id=str(invoice_id),
                final_amount_kopeks=toman_to_kopeks(payload.get('final_amount')),
                credit_amount_kopeks=toman_to_kopeks(payload.get('credit_amount')),
                callback_payload=callback_payload,
            )
            return True

        except Exception as e:
            logger.exception('TonPays callback: processing error', error=e)
            return False

    async def _apply_tonpays_success(
        self,
        db: AsyncSession,
        *,
        payment: Any,
        payload: dict[str, Any],
        event_key: str,
        invoice_id: str,
        callback_payload: dict[str, Any],
    ) -> bool:
        """Verifies the amount and credits the payment. Row lock already held."""
        tonpays_crud = import_module('app.database.crud.tonpays')

        received_kopeks = toman_to_kopeks(payload.get('request_amount'))
        if received_kopeks is None:
            logger.error(
                'TonPays callback: COMPLETED without a parsable request_amount, crediting cancelled',
                order_id=payment.order_id,
                received=payload.get('request_amount'),
            )
            return False

        if received_kopeks != payment.amount_kopeks:
            logger.error(
                'TonPays amount mismatch',
                expected_kopeks=payment.amount_kopeks,
                received_kopeks=received_kopeks,
                order_id=payment.order_id,
            )
            tonpays_crud.remember_tonpays_event(payment, event_key)
            await tonpays_crud.update_tonpays_payment_status(
                db=db,
                payment=payment,
                status='amount_mismatch',
                is_paid=False,
                callback_payload=callback_payload,
            )
            return False

        if payment.is_paid:
            logger.info('TonPays callback: payment already paid', order_id=payment.order_id)
            tonpays_crud.remember_tonpays_event(payment, event_key)
            await db.commit()
            return True

        payment.status = 'success'
        payment.is_paid = True
        payment.paid_at = _parse_provider_ts(payload.get('occurred_at')) or datetime.now(UTC)
        payment.tonpays_payment_id = invoice_id or payment.tonpays_payment_id
        # final_amount is what the buyer paid (request + unique delta);
        # credit_amount is the provider confirmation. We credit the full
        # requested invoice amount and keep both figures for reconciliation.
        final = toman_to_kopeks(payload.get('final_amount'))
        if final is not None:
            payment.final_amount_kopeks = final
        credit = toman_to_kopeks(payload.get('credit_amount'))
        if credit is not None:
            payment.credit_amount_kopeks = credit
        payment.callback_payload = callback_payload
        payment.updated_at = datetime.now(UTC)
        tonpays_crud.remember_tonpays_event(payment, event_key)
        await db.flush()

        return await self._finalize_tonpays_payment(db, payment, trigger='webhook')

    async def _finalize_tonpays_payment(
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
        tonpays_crud = import_module('app.database.crud.tonpays')

        if payment.transaction_id:
            logger.info(
                'TonPays payment already linked to a transaction',
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
            provider_name='tonpays',
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
            logger.error('User not found for TonPays', user_id=payment.user_id)
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
                PaymentMethod.TONPAYS,
            )

        display_name = settings.get_tonpays_display_name()
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
                payment_method=PaymentMethod.TONPAYS,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await tonpays_crud.link_tonpays_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('TonPays payment already credited the balance earlier', order_id=payment.order_id)
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
            payment_method=PaymentMethod.TONPAYS,
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
            logger.error('TonPays referral top-up processing error', error=error)

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
                logger.error('TonPays admin notification error', error=error)

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
                logger.error('TonPays user notification error', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('TonPays saved-cart handling error', user_id=payment.user_id, error=error, exc_info=True)

        metadata['balance_change'] = {
            'old_balance': old_balance,
            'new_balance': user.balance_kopeks,
            'credited_at': datetime.now(UTC).isoformat(),
        }
        metadata['balance_credited'] = True
        payment.metadata_json = metadata
        await db.commit()

        logger.info('TonPays payment processed', order_id=payment.order_id, user_id=payment.user_id, trigger=trigger)

        return True

    async def check_tonpays_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Checks a payment via the TonPays API and syncs the DB.

        The ``paid`` flag of the check response is authoritative (same role as
        HooshPay's verify endpoint). Used by the admin manual check and the
        background reconciliation.
        """
        try:
            tonpays_crud = import_module('app.database.crud.tonpays')
            payment = await tonpays_crud.get_tonpays_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('TonPays payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {'payment': payment, 'status': payment.status, 'is_paid': True}

            if payment.status in TONPAYS_FINAL_STATUSES:
                return {'payment': payment, 'status': payment.status, 'is_paid': False}

            if not payment.tonpays_payment_id:
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            try:
                status_data = await tonpays_service.check_invoice(invoice_id=payment.tonpays_payment_id)
            except Exception as e:
                logger.error('Error checking TonPays payment status via API', error=e)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

            if not status_data:
                logger.warning('TonPays API check: invoice not found at provider', order_id=payment.order_id)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            raw_status = (status_data.get('status') or '').strip().lower()
            paid_flag = status_data.get('paid') is True
            internal_status, _ = TONPAYS_STATUS_MAP.get(raw_status, ('pending', False))
            is_paid = paid_flag and raw_status == 'completed'
            if raw_status == 'completed' and not paid_flag:
                internal_status = 'pending'

            if not is_paid:
                if internal_status != payment.status:
                    final = toman_to_kopeks(status_data.get('final_amount'))
                    payment = await tonpays_crud.update_tonpays_payment_status(
                        db=db,
                        payment=payment,
                        status=internal_status,
                        final_amount_kopeks=final,
                        callback_payload={'check_source': 'api', 'tonpays_status_data': status_data},
                    )
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            received_kopeks = toman_to_kopeks(status_data.get('request_amount'))
            if received_kopeks is None or received_kopeks != payment.amount_kopeks:
                logger.error(
                    'TonPays amount mismatch (API check)',
                    expected_kopeks=payment.amount_kopeks,
                    received_kopeks=received_kopeks,
                    order_id=payment.order_id,
                )
                await tonpays_crud.update_tonpays_payment_status(
                    db=db,
                    payment=payment,
                    status='amount_mismatch',
                    is_paid=False,
                    callback_payload={'check_source': 'api', 'tonpays_status_data': status_data},
                )
                return {'payment': payment, 'status': 'amount_mismatch', 'is_paid': False}

            locked = await tonpays_crud.get_tonpays_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('TonPays: failed to lock payment', payment_id=payment.id)
                return None
            payment = locked

            if payment.is_paid:
                logger.info('TonPays payment already processed (api_check)', order_id=payment.order_id)
                return {'payment': payment, 'status': 'success', 'is_paid': True}

            logger.info('TonPays payment confirmed via API', order_id=payment.order_id)

            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            final = toman_to_kopeks(status_data.get('final_amount'))
            if final is not None:
                payment.final_amount_kopeks = final
            payment.callback_payload = {'check_source': 'api', 'tonpays_status_data': status_data}
            payment.updated_at = datetime.now(UTC)
            tonpays_crud.remember_tonpays_event(payment, f'{payment.tonpays_payment_id}:completed')
            await db.flush()

            await self._finalize_tonpays_payment(db, payment, trigger='api_check')

            return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

        except Exception as e:
            logger.exception('TonPays: status check error', error=e)
            return None

    async def change_tonpays_card(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Requests a new card from TonPays, enforcing the 60s cooldown locally."""
        tonpays_crud = import_module('app.database.crud.tonpays')
        payment = await tonpays_crud.get_tonpays_payment_by_order_id(db, order_id)
        if not payment or not payment.tonpays_payment_id:
            logger.warning('TonPays change card: payment not found', order_id=order_id)
            return None
        if payment.is_paid or payment.status not in TONPAYS_PENDING_STATUSES:
            logger.warning('TonPays change card: payment not changeable', order_id=order_id)
            return None

        cooldown = settings.TONPAYS_CHANGE_CARD_COOLDOWN_SECONDS
        if payment.last_card_change_at:
            last = payment.last_card_change_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            elapsed = (datetime.now(UTC) - last).total_seconds()
            if elapsed < cooldown:
                return {'cooldown_wait': int(cooldown - elapsed), 'payment': payment}

        try:
            result = await tonpays_service.change_card(invoice_id=payment.tonpays_payment_id)
        except Exception as e:
            logger.error('TonPays change card API error', error=e)
            return None
        if not result:
            return None

        payment.card_number = result.get('card_number') or payment.card_number
        payment.card_name = result.get('card_name') or payment.card_name
        payment.last_card_change_at = datetime.now(UTC)
        final = toman_to_kopeks(result.get('final_amount'))
        if final is not None:
            payment.final_amount_kopeks = final
        payment.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(payment)
        return {
            'payment': payment,
            'card_number': payment.card_number,
            'card_name': payment.card_name,
            'show_change_card': result.get('show_change_card', True),
            'change_card_exhausted': bool(result.get('change_card_exhausted', False)),
            'change_card_cooldown_seconds': result.get('change_card_cooldown_seconds', cooldown),
        }

    async def upload_tonpays_receipt(
        self,
        db: AsyncSession,
        order_id: str,
        *,
        file_bytes: bytes,
        filename: str = 'receipt.jpg',
    ) -> dict[str, Any] | None:
        """Uploads the buyer's receipt photo to TonPays (status -> processing)."""
        tonpays_crud = import_module('app.database.crud.tonpays')
        payment = await tonpays_crud.get_tonpays_payment_by_order_id(db, order_id)
        if not payment or not payment.tonpays_payment_id:
            logger.warning('TonPays receipt: payment not found', order_id=order_id)
            return None
        if payment.is_paid or payment.status not in TONPAYS_PENDING_STATUSES:
            logger.warning('TonPays receipt: payment not receivable', order_id=order_id)
            return None

        try:
            result = await tonpays_service.upload_receipt(
                invoice_id=payment.tonpays_payment_id,
                file_bytes=file_bytes,
                filename=filename,
            )
        except Exception as e:
            logger.error('TonPays receipt upload error', error=e)
            return None
        if not result:
            return None

        raw_status = (result.get('status') or 'processing').strip().lower()
        internal_status, _ = TONPAYS_STATUS_MAP.get(raw_status, ('processing', False))
        await tonpays_crud.update_tonpays_payment_status(
            db=db,
            payment=payment,
            status=internal_status,
            receipt_received=bool(result.get('receipt_received', True)),
            callback_payload={'receipt_upload': result},
        )
        return {'payment': payment, 'status': internal_status}
