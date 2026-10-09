"""Mixin for NOWPayments integration (api.nowpayments.io, crypto intake)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.localization.texts import get_texts
from app.services.nowpayments_service import nowpayments_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# NOWPayments payment_status -> (internal status, is_paid). Keys are lowercased.
# ONLY ``finished`` credits: ``confirmed`` means the chain saw the funds but
# they are not ours yet; ``partially_paid`` is an accepted underpayment and
# must go to manual review, never auto-credit.
NOWPAYMENTS_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'waiting': ('pending', False),
    'confirming': ('processing', False),
    'confirmed': ('processing', False),
    'sending': ('processing', False),
    'finished': ('success', True),
    'partially_paid': ('manual_review', False),
    'failed': ('failed', False),
    'refunded': ('refunded', False),
    'expired': ('expired', False),
}

# Statuses the payment can no longer leave.
NOWPAYMENTS_FINAL_STATUSES = frozenset({'amount_mismatch'})

# Statuses from which the payment can still become paid (background reconciliation).
NOWPAYMENTS_PENDING_STATUSES = frozenset({'pending', 'processing'})


class NowpaymentsPaymentMixin:
    """Mixin for working with NOWPayments payments."""

    def _build_result_url(
        self,
        *,
        status: str,
        order_id: str,
        amount_kopeks: int,
        reason: str | None = None,
        note: str | None = None,
        with_plan: bool = False,
        with_support: bool = False,
    ) -> str | None:
        """Builds the static result-page URL for success/cancel/partially URLs.

        The page is display-only (no backend fetch): every value it shows is
        embedded here. Values are display data the payer already knows.

        Length discipline: NOWPayments 500s on long redirect URLs (classic
        VARCHAR(255)-style column — proven by live bisect: 219 chars OK,
        280+ always fails). So: plain ASCII digits (page groups + fa-maps
        them), short ISO date, code values for plan/reason/note (the page
        maps them to Persian), no track (page falls back to invoice), support
        only on fail pages (success uses print, not support).
        """
        base = settings.get_nowpayments_result_page_url()
        if not base:
            return None
        params: dict[str, str] = {
            'status': status,
            'amount': f'{amount_kopeks // 100}',
            'invoice': order_id,
            'date': datetime.now(UTC).strftime('%Y-%m-%d'),
        }
        if with_plan:
            params['plan'] = 'nowpayments'
        if reason:
            params['reason'] = reason
        if note:
            params['note'] = note
        back = self._get_bot_link()
        if back:
            params['back'] = back
        if with_support:
            support = settings.get_support_contact_url()
            if support:
                params['support'] = support
        url = f'{base}?{urlencode(params)}'
        if len(url) > 240:
            logger.warning('NOWPayments result URL is long, may hit provider limits', length=len(url))
        return url

    @staticmethod
    def _get_bot_link() -> str | None:
        username = (getattr(settings, 'BOT_USERNAME', None) or '').strip().lstrip('@')
        if username:
            return f'https://t.me/{username}'
        return None

    async def create_nowpayments_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Balance top-up',
        language: str = 'ru',
    ) -> dict[str, Any] | None:
        """Creates a NOWPayments invoice and returns the hosted pay page URL."""
        if not settings.is_nowpayments_enabled():
            logger.error('NOWPayments is not configured')
            return None

        min_amount = settings.NOWPAYMENTS_MIN_AMOUNT_KOPEKS
        max_amount = settings.NOWPAYMENTS_MAX_AMOUNT_KOPEKS

        if amount_kopeks < min_amount:
            logger.warning(
                'NOWPayments: amount below minimum', amount_kopeks=amount_kopeks, min_kopeks=min_amount
            )
            return None
        if amount_kopeks > max_amount:
            logger.warning(
                'NOWPayments: amount above maximum', amount_kopeks=amount_kopeks, max_kopeks=max_amount
            )
            return None
        if amount_kopeks % 100 != 0:
            logger.warning('NOWPayments: amount is not a whole Toman', amount_kopeks=amount_kopeks)
            return None

        try:
            price_usd = settings.nowpayments_toman_to_usd(amount_kopeks)
        except ValueError as e:
            logger.error('NOWPayments: bad USD rate', error=e)
            return None
        if price_usd <= 0:
            logger.warning('NOWPayments: non-positive USD price', price_usd=price_usd)
            return None

        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            tg_id = None

        order_id = f'np{tg_id or "guest"}_{uuid.uuid4().hex[:8]}'

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'price_usd': price_usd,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            api_result = await nowpayments_service.create_invoice(
                order_id=order_id,
                price_usd=price_usd,
                order_description=(description[:255] if description else None),
                ipn_callback_url=settings.get_nowpayments_callback_url(),
                success_url=self._build_result_url(
                    status='ok', order_id=order_id, amount_kopeks=amount_kopeks, with_plan=True
                ),
                cancel_url=self._build_result_url(
                    status='failed',
                    order_id=order_id,
                    amount_kopeks=amount_kopeks,
                    reason='cancelled',
                    with_support=True,
                ),
                partially_paid_url=self._build_result_url(
                    status='failed',
                    order_id=order_id,
                    amount_kopeks=amount_kopeks,
                    reason='underpaid',
                    note='underpaid',
                    with_support=True,
                ),
            )

            provider_payment_id = api_result.get('id')
            invoice_id = api_result.get('id')
            payment_url = api_result.get('invoice_url')

            nowpayments_crud = import_module('app.database.crud.nowpayments')
            local_payment = await nowpayments_crud.create_nowpayments_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency='IRT',
                description=description,
                price_usd=str(price_usd),
                nowpayments_payment_id=str(provider_payment_id) if provider_payment_id is not None else None,
                invoice_id=str(invoice_id) if invoice_id is not None else None,
                payment_url=payment_url,
                metadata_json=metadata,
            )

            logger.info(
                'NOWPayments: payment created', order_id=order_id, user_id=user_id, amount_kopeks=amount_kopeks
            )

            return {
                'order_id': order_id,
                'provider_payment_id': provider_payment_id,
                'amount_kopeks': amount_kopeks,
                'price_usd': price_usd,
                'currency': 'IRT',
                'payment_url': payment_url,
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('NOWPayments: payment creation error', error=e)
            return None

    async def process_nowpayments_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Handles a NOWPayments IPN (signature already verified in webserver).

        The IPN body mirrors the payment-status response. Idempotent on
        (order_id, payment_status): redelivered events never credit twice.
        """
        try:
            our_order_id = payload.get('order_id')
            provider_payment_id = payload.get('payment_id')
            raw_status = (payload.get('payment_status') or '').strip().lower()

            if not raw_status:
                logger.warning('NOWPayments callback: missing payment_status', payload=payload)
                return False

            nowpayments_crud = import_module('app.database.crud.nowpayments')
            payment = None
            if our_order_id:
                payment = await nowpayments_crud.get_nowpayments_payment_by_order_id(db, str(our_order_id))
            if not payment and provider_payment_id is not None:
                payment = await nowpayments_crud.get_nowpayments_payment_by_provider_id(
                    db, str(provider_payment_id)
                )
            if not payment:
                # A foreign order will not appear on retry — ack the delivery.
                logger.warning('NOWPayments callback: payment not found', order_id=our_order_id)
                return True

            event_key = f'{payment.order_id}:{raw_status}'
            if nowpayments_crud.is_nowpayments_event_processed(payment, event_key):
                logger.info(
                    'NOWPayments callback: event already processed', order_id=payment.order_id, event_key=event_key
                )
                return True

            locked = await nowpayments_crud.get_nowpayments_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('NOWPayments: failed to lock payment', payment_id=payment.id)
                return False
            payment = locked

            # Re-check under the lock: a parallel delivery of the same event could
            # have slipped between the read and the row lock.
            if nowpayments_crud.is_nowpayments_event_processed(payment, event_key):
                logger.info('NOWPayments callback: event already processed (locked)', event_key=event_key)
                return True

            if raw_status not in NOWPAYMENTS_STATUS_MAP:
                logger.warning(
                    'NOWPayments callback: unknown status', order_id=payment.order_id, status=raw_status
                )
                return True

            if payment.status in NOWPAYMENTS_FINAL_STATUSES:
                logger.warning(
                    'NOWPayments callback: payment in final status, event ignored',
                    order_id=payment.order_id,
                    current_status=payment.status,
                    incoming_status=raw_status,
                )
                nowpayments_crud.remember_nowpayments_event(payment, event_key)
                await db.commit()
                return True

            internal_status, is_paid = NOWPAYMENTS_STATUS_MAP[raw_status]

            callback_payload = {
                'payment_id': provider_payment_id,
                'payment_status': raw_status,
                'price_amount': payload.get('price_amount'),
                'price_currency': payload.get('price_currency'),
                'pay_amount': payload.get('pay_amount'),
                'actually_paid': payload.get('actually_paid'),
                'pay_currency': payload.get('pay_currency'),
                'outcome_amount': payload.get('outcome_amount'),
                'outcome_currency': payload.get('outcome_currency'),
            }

            if is_paid:
                return await self._apply_nowpayments_success(
                    db,
                    payment=payment,
                    event_key=event_key,
                    provider_payment_id=str(provider_payment_id)
                    if provider_payment_id is not None
                    else payment.nowpayments_payment_id,
                    callback_payload=callback_payload,
                )

            if payment.is_paid:
                # Late/delayed non-paid delivery must never regress a credited
                # payment. Acknowledge and keep state.
                logger.warning(
                    'NOWPayments callback: non-paid event for already-paid payment, ignored',
                    order_id=payment.order_id,
                    current_status=payment.status,
                    incoming_status=raw_status,
                )
                nowpayments_crud.remember_nowpayments_event(payment, event_key)
                await db.commit()
                return True

            if internal_status == 'manual_review':
                # Accepted underpayment — NEVER auto-credit. Park for admin.
                logger.error(
                    'NOWPayments partially paid, manual review required',
                    order_id=payment.order_id,
                    actually_paid=payload.get('actually_paid'),
                )
                nowpayments_crud.remember_nowpayments_event(payment, event_key)
                await nowpayments_crud.update_nowpayments_payment_status(
                    db=db,
                    payment=payment,
                    status='manual_review',
                    is_paid=False,
                    nowpayments_payment_id=str(provider_payment_id)
                    if provider_payment_id is not None
                    else None,
                    pay_currency=str(payload.get('pay_currency'))
                    if payload.get('pay_currency') is not None
                    else None,
                    pay_amount=str(payload.get('pay_amount'))
                    if payload.get('pay_amount') is not None
                    else None,
                    actually_paid=str(payload.get('actually_paid'))
                    if payload.get('actually_paid') is not None
                    else None,
                    outcome_currency=str(payload.get('outcome_currency'))
                    if payload.get('outcome_currency') is not None
                    else None,
                    outcome_amount=str(payload.get('outcome_amount'))
                    if payload.get('outcome_amount') is not None
                    else None,
                    callback_payload=callback_payload,
                )
                return True

            nowpayments_crud.remember_nowpayments_event(payment, event_key)
            await nowpayments_crud.update_nowpayments_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=None,
                nowpayments_payment_id=str(provider_payment_id)
                if provider_payment_id is not None
                else None,
                pay_currency=str(payload.get('pay_currency'))
                if payload.get('pay_currency') is not None
                else None,
                pay_amount=str(payload.get('pay_amount')) if payload.get('pay_amount') is not None else None,
                callback_payload=callback_payload,
            )
            return True

        except Exception as e:
            logger.exception('NOWPayments callback: processing error', error=e)
            return False

    async def _apply_nowpayments_success(
        self,
        db: AsyncSession,
        *,
        payment: Any,
        event_key: str,
        provider_payment_id: str | None,
        callback_payload: dict[str, Any],
    ) -> bool:
        """Credits a finished payment. Row lock already held.

        Credits the requested Toman amount: the crypto legs are floats with
        rate drift, so no strict equality check applies — linkage is by
        order/payment id instead.
        """
        nowpayments_crud = import_module('app.database.crud.nowpayments')

        if payment.is_paid:
            logger.info('NOWPayments callback: payment already paid', order_id=payment.order_id)
            nowpayments_crud.remember_nowpayments_event(payment, event_key)
            await db.commit()
            return True

        payment.status = 'success'
        payment.is_paid = True
        payment.paid_at = datetime.now(UTC)
        if provider_payment_id:
            payment.nowpayments_payment_id = provider_payment_id
        pay_currency = callback_payload.get('pay_currency')
        if pay_currency is not None:
            payment.pay_currency = str(pay_currency)
        pay_amount = callback_payload.get('pay_amount')
        if pay_amount is not None:
            payment.pay_amount = str(pay_amount)
        actually_paid = callback_payload.get('actually_paid')
        if actually_paid is not None:
            payment.actually_paid = str(actually_paid)
        outcome_currency = callback_payload.get('outcome_currency')
        if outcome_currency is not None:
            payment.outcome_currency = str(outcome_currency)
        outcome_amount = callback_payload.get('outcome_amount')
        if outcome_amount is not None:
            payment.outcome_amount = str(outcome_amount)
        payment.callback_payload = callback_payload
        payment.updated_at = datetime.now(UTC)
        nowpayments_crud.remember_nowpayments_event(payment, event_key)
        await db.flush()

        return await self._finalize_nowpayments_payment(db, payment, trigger='webhook')

    async def _finalize_nowpayments_payment(
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
        nowpayments_crud = import_module('app.database.crud.nowpayments')

        if payment.transaction_id:
            logger.info(
                'NOWPayments payment already linked to a transaction',
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
            provider_name='nowpayments',
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
            logger.error('User not found for NOWPayments', user_id=payment.user_id)
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
                PaymentMethod.NOWPAYMENTS,
            )

        display_name = settings.get_nowpayments_display_name()
        texts = get_texts(getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE)
        description = texts.t(
            'NOWPAYMENTS_TRANSACTION_DESCRIPTION', 'Пополнение через {display_name}'
        ).format(display_name=display_name)

        transaction = existing_transaction
        created_transaction = False

        if not transaction:
            transaction = await payment_module.create_transaction(
                db,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=payment.amount_kopeks,
                description=description,
                payment_method=PaymentMethod.NOWPAYMENTS,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await nowpayments_crud.link_nowpayments_payment_to_transaction(
            db, payment=payment, transaction_id=transaction.id
        )

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('NOWPayments payment already credited the balance earlier', order_id=payment.order_id)
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
            payment_method=PaymentMethod.NOWPAYMENTS,
            external_id=transaction_external_id,
        )

        topup_status = (
            texts.t('SEVERPAY_TOPUP_STATUS_FIRST', '\U0001f195 Первое пополнение')
            if was_first_topup
            else texts.t('SEVERPAY_TOPUP_STATUS_REPEAT', '\U0001f504 Пополнение')
        )

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                payment.amount_kopeks,
                getattr(self, 'bot', None),
            )
        except Exception as error:
            logger.error('NOWPayments referral top-up processing error', error=error)

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
                logger.error('NOWPayments admin notification error', error=error)

        if getattr(self, 'bot', None) and user.telegram_id and settings.is_notifications_enabled():
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    texts.t(
                        'NOWPAYMENTS_TOPUP_SUCCESS',
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        '\U0001f4b0 Сумма: {amount}\n'
                        '\U0001f4b3 Способ: {method}\n'
                        '\U0001f194 Транзакция: {transaction_id}\n\n'
                        'Баланс пополнен автоматически!',
                    ).format(
                        amount=settings.format_price(payment.amount_kopeks),
                        method=display_name,
                        transaction_id=transaction.id,
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('NOWPayments user notification error', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error(
                'NOWPayments saved-cart handling error', user_id=payment.user_id, error=error, exc_info=True
            )

        metadata['balance_change'] = {
            'old_balance': old_balance,
            'new_balance': user.balance_kopeks,
            'credited_at': datetime.now(UTC).isoformat(),
        }
        metadata['balance_credited'] = True
        payment.metadata_json = metadata
        await db.commit()

        logger.info(
            'NOWPayments payment processed', order_id=payment.order_id, user_id=payment.user_id, trigger=trigger
        )

        return True

    async def check_nowpayments_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Checks a payment via the NOWPayments API and syncs the DB.

        Only ``finished`` credits. Used by the «paid» button, the admin manual
        check and the background reconciliation.
        """
        try:
            nowpayments_crud = import_module('app.database.crud.nowpayments')
            payment = await nowpayments_crud.get_nowpayments_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('NOWPayments payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {'payment': payment, 'status': payment.status, 'is_paid': True}

            if payment.status in NOWPAYMENTS_FINAL_STATUSES:
                return {'payment': payment, 'status': payment.status, 'is_paid': False}

            if not payment.nowpayments_payment_id:
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            try:
                status_data = await nowpayments_service.get_payment_status(
                    payment_id=payment.nowpayments_payment_id
                )
            except Exception as e:
                logger.error('Error checking NOWPayments payment status via API', error=e)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

            if not status_data:
                logger.warning('NOWPayments API check: payment not found at provider', order_id=payment.order_id)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            raw_status = (status_data.get('payment_status') or '').strip().lower()
            internal_status, is_paid = NOWPAYMENTS_STATUS_MAP.get(raw_status, ('pending', False))

            if not is_paid:
                if internal_status != payment.status:
                    payment = await nowpayments_crud.update_nowpayments_payment_status(
                        db=db,
                        payment=payment,
                        status=internal_status,
                        pay_currency=str(status_data.get('pay_currency'))
                        if status_data.get('pay_currency') is not None
                        else None,
                        pay_amount=str(status_data.get('pay_amount'))
                        if status_data.get('pay_amount') is not None
                        else None,
                        callback_payload={'check_source': 'api', 'nowpayments_status_data': status_data},
                    )
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            locked = await nowpayments_crud.get_nowpayments_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('NOWPayments: failed to lock payment', payment_id=payment.id)
                return None
            payment = locked

            if payment.is_paid:
                logger.info('NOWPayments payment already processed (api_check)', order_id=payment.order_id)
                return {'payment': payment, 'status': 'success', 'is_paid': True}

            logger.info('NOWPayments payment confirmed via API', order_id=payment.order_id)

            event_key = f'{payment.order_id}:{raw_status}'
            nowpayments_crud.remember_nowpayments_event(payment, event_key)

            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            if status_data.get('pay_currency') is not None:
                payment.pay_currency = str(status_data.get('pay_currency'))
            if status_data.get('pay_amount') is not None:
                payment.pay_amount = str(status_data.get('pay_amount'))
            if status_data.get('actually_paid') is not None:
                payment.actually_paid = str(status_data.get('actually_paid'))
            if status_data.get('outcome_currency') is not None:
                payment.outcome_currency = str(status_data.get('outcome_currency'))
            if status_data.get('outcome_amount') is not None:
                payment.outcome_amount = str(status_data.get('outcome_amount'))
            payment.callback_payload = {'check_source': 'api', 'nowpayments_status_data': status_data}
            payment.updated_at = datetime.now(UTC)
            await db.flush()

            await self._finalize_nowpayments_payment(db, payment, trigger='api_check')

            return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

        except Exception as e:
            logger.exception('NOWPayments: status check error', error=e)
            return None
