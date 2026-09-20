import asyncio
import html
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog
from aiogram import Bot, types
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.exc import MissingGreenlet, NoInspectionAvailable
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_group_by_id
from app.database.crud.subscription_event import create_subscription_event
from app.database.crud.transaction import get_transaction_by_id
from app.database.crud.user import get_user_by_id
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    GuestPurchase,
    PromoCodeType,
    PromoGroup,
    Subscription,
    Transaction,
    User,
)
from app.localization.texts import get_texts
from app.utils.formatters import format_username_link
from app.utils.message_patch import caption_exceeds_telegram_limit
from app.utils.rich_admin import classic_admin_html_to_rich, try_send_rich_admin_message
from app.utils.timezone import format_local_datetime


# Стандартный формат Telegram bot token: `<numeric_id>:<random_35chars>`.
# Может появиться в str(e) от aiogram при сетевых ошибках, если транспорт
# (httpx/aiohttp) сериализует URL `https://api.telegram.org/bot<TOKEN>/...`.
# Не светим токен в логи (структурированные логи могут уехать в Sentry / ELK).
# Trailing — negative lookahead, а не `\b`: иначе токены, оканчивающиеся
# на `-` или `_`, теряли последний символ при редакции (1-char leak).
# Leading `(?<![\w-])` — намеренно НЕ матчит, если перед токеном стоит word/digit
# (например `foo123456789:AAH...`). Это trade-off против false-positive'ов
# на timestamp/UUID-подобных последовательностях. Aiogram и httpx всегда
# префиксят токен либо `bot`, либо URL-границей (`/`, пробел, кавычка),
# так что реальный corpus ошибок не страдает.
_BOT_TOKEN_RE: re.Pattern[str] = re.compile(
    r'(?<![\w-])(?:bot)?\d{6,}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])',
)


def _redact_telegram_secrets(text: str) -> str:
    """Replace Telegram bot tokens in an arbitrary string with a placeholder."""
    return _BOT_TOKEN_RE.sub('bot[REDACTED]', text)


class NotificationCategory(StrEnum):
    """Категории уведомлений для маршрутизации по топикам."""

    PURCHASES = 'purchases'  # Покупки подписок, покупки с лендинга
    RENEWALS = 'renewals'  # Продления
    TRIALS = 'trials'  # Триалы
    BALANCE = 'balance'  # Пополнение баланса
    ADDONS = 'addons'  # Докупка трафика/устройств/серверов
    INFRASTRUCTURE = 'infrastructure'  # Ноды, техработы, статус панели, вебхуки
    ERRORS = 'errors'  # Ошибки бота, краши
    PROMO = 'promo'  # Промокоды, кампании, промогруппы
    PARTNERS = 'partners'  # Партнёрки, выводы, админ-действия
    TICKETS = 'tickets'  # Тикеты (уже существует)


logger = structlog.get_logger(__name__)


def _loaded_relationship(instance: object, name: str) -> Any:
    """Значение связи, только если она УЖЕ загружена; иначе None и никакого IO.

    ``getattr(obj, name, None)`` для этого не годится: у незагруженной связи
    async-сессия не отдаёт None, а лезет в базу — и падает MissingGreenlet, потому
    что default у getattr срабатывает лишь на AttributeError.

    Ровно на этом падало уведомление о регистрации по рекламной кампании:
    apply_campaign_bonus перечитывает пользователя через ``db.refresh(user)``
    (сам по себе — фикс прошлого MissingGreenlet), а refresh сбрасывает ранее
    загруженные связи, включая promo_group.
    """
    try:
        state = sa_inspect(instance)
    except NoInspectionAvailable:
        # Не ORM-объект (тестовые фейки, SimpleNamespace) — обычный доступ безопасен.
        return getattr(instance, name, None)

    if name in state.unloaded:
        return None
    return state.dict.get(name)


class AdminNotificationService:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.chat_id = getattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None)
        self.topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None)
        self.ticket_topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID', None)
        self.enabled = getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)

        # Маппинг категорий на topic_id (None = fallback на self.topic_id)
        self.category_topics: dict[NotificationCategory, int | None] = {
            NotificationCategory.PURCHASES: getattr(settings, 'ADMIN_NOTIFICATIONS_PURCHASES_TOPIC_ID', None),
            NotificationCategory.RENEWALS: getattr(settings, 'ADMIN_NOTIFICATIONS_RENEWALS_TOPIC_ID', None),
            NotificationCategory.TRIALS: getattr(settings, 'ADMIN_NOTIFICATIONS_TRIALS_TOPIC_ID', None),
            NotificationCategory.BALANCE: getattr(settings, 'ADMIN_NOTIFICATIONS_BALANCE_TOPIC_ID', None),
            NotificationCategory.ADDONS: getattr(settings, 'ADMIN_NOTIFICATIONS_ADDONS_TOPIC_ID', None),
            NotificationCategory.INFRASTRUCTURE: getattr(settings, 'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_TOPIC_ID', None),
            NotificationCategory.ERRORS: getattr(settings, 'ADMIN_NOTIFICATIONS_ERRORS_TOPIC_ID', None),
            NotificationCategory.PROMO: getattr(settings, 'ADMIN_NOTIFICATIONS_PROMO_TOPIC_ID', None),
            NotificationCategory.PARTNERS: getattr(settings, 'ADMIN_NOTIFICATIONS_PARTNERS_TOPIC_ID', None),
            NotificationCategory.TICKETS: self.ticket_topic_id,
        }

        # Per-category enabled flags (default True — backwards compatible)
        self.category_enabled: dict[NotificationCategory, bool] = {}
        for cat in NotificationCategory:
            key = f'ADMIN_NOTIFICATIONS_{cat.value.upper()}_ENABLED'
            self.category_enabled[cat] = getattr(settings, key, True)

    async def _get_referrer_info(self, db: AsyncSession, referred_by_id: int | None) -> str:
        if not referred_by_id:
            return 'Нет'

        try:
            referrer = await get_user_by_id(db, referred_by_id)
            if not referrer:
                return get_texts().t('ADMIN_NOTIFY_REFERRER_NOT_FOUND', 'ID {user_id} (не найден)').format(
                    user_id=referred_by_id
                )

            if referrer.username:
                return f'{format_username_link(referrer.username)} (ID: {referred_by_id})'
            if referrer.telegram_id:
                return f'ID {referrer.telegram_id}'
            if referrer.email:
                return f'📧 {html.escape(referrer.email)}'
            return f'User#{referred_by_id}'

        except Exception as e:
            logger.error('Ошибка получения данных рефера', referred_by_id=referred_by_id, error=e)
            return f'ID {referred_by_id}'

    async def _get_user_promo_group(self, db: AsyncSession, user: User) -> PromoGroup | None:
        promo_group = _loaded_relationship(user, 'promo_group')
        if promo_group:
            return promo_group

        try:
            promo_group_id = user.promo_group_id
        except Exception:
            # Инстанс отвязан от сессии или протух — колонка тоже ушла бы в ленивую
            # подгрузку. Берём последнее известное значение из __dict__.
            promo_group_id = user.__dict__.get('promo_group_id')
        if not promo_group_id:
            return None

        try:
            await db.refresh(user, attribute_names=['promo_group'])
        except Exception:
            # relationship might not be available — fallback to direct fetch
            pass

        promo_group = _loaded_relationship(user, 'promo_group')
        if promo_group:
            return promo_group

        try:
            return await get_promo_group_by_id(db, promo_group_id)
        except Exception as e:
            logger.error(
                'Ошибка загрузки промогруппы пользователя',
                promo_group_id=promo_group_id,
                telegram_id=user.__dict__.get('telegram_id'),
                e=e,
            )
            return None

    def _get_user_display(self, user: User) -> str:
        first_name = getattr(user, 'first_name', '') or ''
        if first_name:
            return html.escape(first_name)

        username = getattr(user, 'username', '') or ''
        if username:
            return html.escape(username)

        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id is None:
            email = getattr(user, 'email', None)
            if email:
                return html.escape(email)
            return f'User#{getattr(user, "id", "Unknown")}'
        return f'ID{telegram_id}'

    def _get_user_identifier_display(self, user: User) -> str:
        """Get user identifier for display in notifications (telegram_id or email)."""
        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id:
            return f'<code>{telegram_id}</code>'

        email = getattr(user, 'email', None)
        if email:
            return f'📧 {html.escape(email)}'

        return f'User#{getattr(user, "id", "Unknown")}'

    def _get_user_identifier_label(self, user: User) -> str:
        """Get label for user identifier (Telegram ID or Email)."""
        texts = get_texts()
        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id:
            return texts.t('ADMIN_NOTIFY_LABEL_TELEGRAM_ID', 'Telegram ID')
        email = getattr(user, 'email', None)
        if email:
            return texts.t('ADMIN_NOTIFY_LABEL_EMAIL', 'Email')
        return texts.t('ADMIN_NOTIFY_LABEL_ID', 'ID')

    async def _record_subscription_event(
        self,
        db: AsyncSession,
        *,
        event_type: str,
        user: User,
        subscription: Subscription | None,
        transaction: Transaction | None = None,
        amount_kopeks: int | None = None,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """Persist subscription-related event for external dashboards."""

        try:
            await create_subscription_event(
                db,
                user_id=user.id,
                event_type=event_type,
                subscription_id=subscription.id if subscription else None,
                transaction_id=transaction.id if transaction else None,
                amount_kopeks=amount_kopeks,
                currency=None,
                message=message,
                occurred_at=occurred_at,
                extra=extra or None,
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие подписки для пользователя',
                event_type=event_type,
                getattr=getattr(user, 'id', 'unknown'),
                exc_info=True,
            )

            try:
                await db.rollback()
            except Exception:
                logger.error(
                    'Не удалось выполнить rollback после ошибки события подписки пользователя',
                    getattr=getattr(user, 'id', 'unknown'),
                    exc_info=True,
                )

    def _format_promo_group_discounts(self, promo_group: PromoGroup) -> list[str]:
        texts = get_texts()
        discount_lines: list[str] = []

        discount_map = {
            'servers': (
                texts.t('ADMIN_NOTIFY_DISCOUNT_SERVERS', 'Серверы'),
                promo_group.server_discount_percent,
            ),
            'traffic': (
                texts.t('ADMIN_NOTIFY_DISCOUNT_TRAFFIC', 'Трафик'),
                promo_group.traffic_discount_percent,
            ),
            'devices': (
                texts.t('ADMIN_NOTIFY_DISCOUNT_DEVICES', 'Устройства'),
                promo_group.device_discount_percent,
            ),
        }

        for title, percent in discount_map.values():
            if percent and percent > 0:
                discount_lines.append(
                    texts.t('ADMIN_NOTIFY_DISCOUNT_LINE', '• {title}: -{percent}%').format(
                        title=title, percent=percent
                    )
                )

        period_discounts_raw = promo_group.period_discounts or {}
        period_items: list[tuple[int, int]] = []

        if isinstance(period_discounts_raw, dict):
            for raw_days, raw_percent in period_discounts_raw.items():
                try:
                    days = int(raw_days)
                    percent = int(raw_percent)
                except (TypeError, ValueError):
                    continue

                if percent > 0:
                    period_items.append((days, percent))

        period_items.sort(key=lambda item: item[0])

        if period_items:
            period_template = texts.t('ADMIN_NOTIFY_DISCOUNT_PERIOD_ITEM', '{days} д. — -{percent}%')
            formatted_periods = ', '.join(
                period_template.format(days=days, percent=percent) for days, percent in period_items
            )
            discount_lines.append(
                texts.t('ADMIN_NOTIFY_DISCOUNT_PERIODS_LINE', '• Периоды: {periods}').format(
                    periods=formatted_periods
                )
            )

        if promo_group.apply_discounts_to_addons:
            discount_lines.append(
                texts.t('ADMIN_NOTIFY_DISCOUNT_ADDONS_ON', '• Доп. услуги: ✅ скидка действует')
            )
        else:
            discount_lines.append(
                texts.t('ADMIN_NOTIFY_DISCOUNT_ADDONS_OFF', '• Доп. услуги: ❌ без скидки')
            )

        return discount_lines

    def _format_promo_group_block(
        self,
        promo_group: PromoGroup | None,
        *,
        title: str | None = None,
        icon: str = '🏷️',
    ) -> str:
        texts = get_texts()
        if title is None:
            title = texts.t('ADMIN_NOTIFY_PROMO_GROUP_TITLE', 'Промогруппа')

        if not promo_group:
            return texts.t('ADMIN_NOTIFY_PROMO_GROUP_EMPTY', '{icon} <b>{title}:</b> —').format(
                icon=icon, title=title
            )

        lines = [
            texts.t('ADMIN_NOTIFY_PROMO_GROUP_HEADER', '{icon} <b>{title}:</b> {name}').format(
                icon=icon, title=title, name=html.escape(promo_group.name)
            )
        ]

        discount_lines = self._format_promo_group_discounts(promo_group)
        if discount_lines:
            lines.append(texts.t('ADMIN_NOTIFY_DISCOUNTS_HEADER', '💸 <b>Скидки:</b>'))
            lines.extend(discount_lines)
        else:
            lines.append(
                texts.t('ADMIN_NOTIFY_DISCOUNTS_NONE', '💸 <b>Скидки:</b> отсутствуют')
            )

        return '\n'.join(lines)

    def _get_promocode_type_display(self, promo_type: str | None) -> str:
        texts = get_texts()
        mapping = {
            PromoCodeType.BALANCE.value: texts.t('ADMIN_NOTIFY_PROMO_TYPE_BALANCE', '💰 Бонус на баланс'),
            PromoCodeType.SUBSCRIPTION_DAYS.value: texts.t(
                'ADMIN_NOTIFY_PROMO_TYPE_DAYS', '⏰ Доп. дни подписки'
            ),
            PromoCodeType.TRIAL_SUBSCRIPTION.value: texts.t(
                'ADMIN_NOTIFY_PROMO_TYPE_TRIAL', '🎁 Триал подписка'
            ),
            PromoCodeType.PROMO_GROUP.value: texts.t('ADMIN_NOTIFY_PROMO_TYPE_GROUP', '👥 Промогруппа'),
            PromoCodeType.DISCOUNT.value: texts.t('ADMIN_NOTIFY_PROMO_TYPE_DISCOUNT', '💸 Скидка'),
            PromoCodeType.BALANCE_AND_DAYS.value: texts.t(
                'ADMIN_NOTIFY_PROMO_TYPE_BALANCE_AND_DAYS', '💰📅 Баланс + дни подписки'
            ),
        }

        if not promo_type:
            return texts.t('ADMIN_NOTIFY_PROMO_TYPE_UNSET', 'ℹ️ Не указан')

        return mapping.get(promo_type, f'ℹ️ {promo_type}')

    def _format_campaign_bonus(self, campaign: AdvertisingCampaign, *, tariff_name: str | None = None) -> list[str]:
        texts = get_texts()
        if campaign.is_balance_bonus:
            return [
                texts.t('ADMIN_NOTIFY_CAMPAIGN_BALANCE', '💰 Баланс: {amount}').format(
                    amount=settings.format_price(campaign.balance_bonus_kopeks or 0)
                ),
            ]

        if campaign.is_subscription_bonus:
            default_devices = getattr(settings, 'DEFAULT_DEVICE_LIMIT', 1)
            details = [
                texts.t(
                    'ADMIN_NOTIFY_CAMPAIGN_SUBSCRIPTION',
                    '📅 {days} дн. • 📊 {traffic} ГБ • 📱 {devices} устр.',
                ).format(
                    days=campaign.subscription_duration_days or 0,
                    traffic=campaign.subscription_traffic_gb or 0,
                    devices=campaign.subscription_device_limit or default_devices,
                ),
            ]
            if campaign.subscription_squads:
                details.append(
                    texts.t('ADMIN_NOTIFY_CAMPAIGN_SQUADS', '🌐 Сквады: {count} шт.').format(
                        count=len(campaign.subscription_squads)
                    )
                )
            return details

        if campaign.is_tariff_bonus:
            name = tariff_name or f'ID {campaign.tariff_id}'
            details = [
                texts.t('ADMIN_NOTIFY_CAMPAIGN_TARIFF', '📦 Тариф: <b>{name}</b>').format(name=name)
            ]
            if campaign.tariff_duration_days:
                details.append(
                    texts.t('ADMIN_NOTIFY_CAMPAIGN_TARIFF_PERIOD', '📅 Период: {days} дней').format(
                        days=campaign.tariff_duration_days
                    )
                )
            return details

        if campaign.is_none_bonus:
            return [texts.t('ADMIN_NOTIFY_CAMPAIGN_TRACKING_ONLY', '🔗 Только отслеживание')]

        return [texts.t('ADMIN_NOTIFY_CAMPAIGN_NO_BONUS', 'ℹ️ Бонусы не предусмотрены')]

    async def send_trial_activation_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        *,
        charged_amount_kopeks: int | None = None,
    ) -> bool:
        try:
            await self._record_subscription_event(
                db,
                event_type='activation',
                user=user,
                subscription=subscription,
                transaction=None,
                amount_kopeks=charged_amount_kopeks,
                message='Trial activation',
                occurred_at=datetime.now(UTC),
                extra={
                    'charged_amount_kopeks': charged_amount_kopeks,
                    'trial_duration_days': (
                        max(1, round((subscription.end_date - subscription.start_date).total_seconds() / 86400))
                        if subscription.end_date and subscription.start_date
                        else settings.TRIAL_DURATION_DAYS
                    ),
                    'traffic_limit_gb': (
                        subscription.traffic_limit_gb
                        if subscription.traffic_limit_gb is not None
                        else settings.TRIAL_TRAFFIC_LIMIT_GB
                    ),
                    'device_limit': subscription.device_limit,
                },
            )

            if not self._is_enabled():
                return False

            texts = get_texts()
            user_status = (
                texts.t('ADMIN_NOTIFY_USER_STATUS_NEW', '🆕 Новый')
                if not user.has_had_paid_subscription
                else texts.t('ADMIN_NOTIFY_USER_STATUS_EXISTING', '🔄 Существующий')
            )
            promo_group = await self._get_user_promo_group(db, user)
            user_display = self._get_user_display(user)

            trial_device_limit = subscription.device_limit
            if trial_device_limit is None:
                fallback_forced_limit = settings.get_disabled_mode_device_limit()
                if fallback_forced_limit is not None:
                    trial_device_limit = fallback_forced_limit
                else:
                    trial_device_limit = settings.TRIAL_DEVICE_LIMIT

            payment_block = ''
            if charged_amount_kopeks and charged_amount_kopeks > 0:
                payment_block = texts.t(
                    'ADMIN_NOTIFY_TRIAL_PAYMENT_LINE', '\n💳 <b>Оплата за активацию:</b> {amount}'
                ).format(amount=settings.format_price(charged_amount_kopeks))

            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            # Получаем название тарифа (если режим тарифов)
            tariff_name = await self._get_tariff_name(db, subscription)

            message_lines = [
                texts.t('ADMIN_NOTIFY_TRIAL_TITLE', '🎯 <b>АКТИВАЦИЯ ТРИАЛА</b>'),
                '',
                texts.t('ADMIN_NOTIFY_USER_LINE', '👤 <b>Пользователь:</b> {user}').format(user=user_display),
                texts.t('ADMIN_NOTIFY_USER_ID_LINE', '🆔 <b>{label}:</b> {value}').format(
                    label=user_id_label, value=user_id_display
                ),
                texts.t('ADMIN_NOTIFY_USERNAME_LINE', '📱 <b>Username:</b> {username}').format(
                    username=format_username_link(
                        getattr(user, 'username', None),
                        texts.t('ADMIN_NOTIFY_USERNAME_MISSING', 'отсутствует'),
                    )
                ),
                texts.t('ADMIN_NOTIFY_STATUS_LINE', '👥 <b>Статус:</b> {status}').format(status=user_status),
                '',
            ]

            # Промогруппа — только название, без скидок
            if promo_group:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_PROMO_GROUP_LINE', '🏷️ <b>Промогруппа:</b> {name}').format(
                        name=html.escape(promo_group.name)
                    )
                )
            else:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_PROMO_GROUP_LINE_EMPTY', '🏷️ <b>Промогруппа:</b> —')
                )

            # Тариф триала (если есть)
            if tariff_name:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_TARIFF_LINE', '📦 <b>Тариф:</b> {name}').format(name=tariff_name)
                )

            message_lines.append('')

            trial_duration_days = settings.TRIAL_DURATION_DAYS
            if subscription.end_date and subscription.start_date:
                trial_duration_days = max(
                    1, round((subscription.end_date - subscription.start_date).total_seconds() / 86400)
                )

            trial_traffic_gb = (
                subscription.traffic_limit_gb
                if subscription.traffic_limit_gb is not None
                else settings.TRIAL_TRAFFIC_LIMIT_GB
            )

            message_lines.extend(
                [
                    texts.t('ADMIN_NOTIFY_TRIAL_PARAMS_HEADER', '⏰ <b>Параметры триала:</b>'),
                    texts.t('ADMIN_NOTIFY_PERIOD_DAYS_LINE', '📅 Период: {days} дней').format(
                        days=trial_duration_days
                    ),
                    texts.t('ADMIN_NOTIFY_TRAFFIC_LINE', '📊 Трафик: {traffic}').format(
                        traffic=self._format_traffic(trial_traffic_gb)
                    ),
                    texts.t('ADMIN_NOTIFY_DEVICES_LINE', '📱 Устройства: {devices}').format(
                        devices=trial_device_limit
                    ),
                    texts.t('ADMIN_NOTIFY_SERVER_LINE', '🌐 Сервер: {server}').format(
                        server=(
                            subscription.connected_squads[0]
                            if subscription.connected_squads
                            else texts.t('ADMIN_NOTIFY_SERVER_DEFAULT', 'По умолчанию')
                        )
                    ),
                ]
            )

            if payment_block:
                message_lines.append(payment_block)

            message_lines.append('')
            message_lines.append(
                texts.t('ADMIN_NOTIFY_VALID_UNTIL_LINE', '📆 <b>Действует до:</b> {date}').format(
                    date=format_local_datetime(subscription.end_date, '%d.%m.%Y %H:%M')
                )
            )

            # Реферер — только если есть
            if user.referred_by_id:
                referrer_info = await self._get_referrer_info(db, user.referred_by_id)
                if referrer_info != 'Нет':
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_REFERRER_LINE', '🔗 <b>Реферер:</b> {referrer}').format(
                            referrer=referrer_info
                        )
                    )

            message_lines.append('')
            message_lines.append(
                texts.t('ADMIN_NOTIFY_TIMESTAMP_LINE', '⏰ <i>{timestamp}</i>').format(
                    timestamp=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
                )
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.TRIALS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о триале', error=e)
            return False

    async def _get_tariff_name(self, db: AsyncSession, subscription: Subscription) -> str | None:
        """Получает название тарифа подписки, если он есть."""
        if not subscription.tariff_id:
            return None

        try:
            from app.database.crud.tariff import get_tariff_by_id

            tariff = await get_tariff_by_id(db, subscription.tariff_id)
            if tariff:
                return html.escape(tariff.name)
        except Exception:
            pass
        return None

    async def send_subscription_purchase_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        transaction: Transaction | None,
        period_days: int,
        was_trial_conversion: bool = False,
        amount_kopeks: int | None = None,
        purchase_type: str | None = None,  # 'first_purchase', 'renewal', 'tariff_switch', None (auto-detect)
    ) -> bool:
        try:
            total_amount = (
                amount_kopeks if amount_kopeks is not None else (abs(transaction.amount_kopeks) if transaction else 0)
            )

            await self._record_subscription_event(
                db,
                event_type='purchase',
                user=user,
                subscription=subscription,
                transaction=transaction,
                amount_kopeks=total_amount,
                message='Subscription purchase',
                occurred_at=(transaction.completed_at or transaction.created_at) if transaction else datetime.now(UTC),
                extra={
                    'period_days': period_days,
                    'was_trial_conversion': was_trial_conversion,
                    'payment_method': self._get_payment_method_display(transaction.payment_method)
                    if transaction
                    else 'Баланс',
                },
            )

            if not self._is_enabled():
                return False

            # Определяем тип операции и заголовок
            texts = get_texts()
            if purchase_type == 'tariff_switch':
                event_title = texts.t('ADMIN_NOTIFY_PURCHASE_TITLE_TARIFF_SWITCH', '🔄 СМЕНА ТАРИФА')
                user_status = texts.t('ADMIN_NOTIFY_PURCHASE_STATUS_TARIFF_SWITCH', 'Смена тарифа')
            elif was_trial_conversion:
                event_title = texts.t('ADMIN_NOTIFY_PURCHASE_TITLE_CONVERSION', '🔄 КОНВЕРСИЯ ИЗ ТРИАЛА')
                user_status = texts.t('ADMIN_NOTIFY_PURCHASE_STATUS_CONVERSION', 'Конверсия')
            elif purchase_type == 'first_purchase':
                event_title = texts.t('ADMIN_NOTIFY_PURCHASE_TITLE_PURCHASE', '💎 ПОКУПКА ПОДПИСКИ')
                user_status = texts.t('ADMIN_NOTIFY_PURCHASE_STATUS_FIRST', 'Первая покупка')
            elif purchase_type == 'renewal' or (purchase_type is None and user.has_had_paid_subscription):
                event_title = texts.t('ADMIN_NOTIFY_PURCHASE_TITLE_RENEWAL', '💎 ПРОДЛЕНИЕ ПОДПИСКИ')
                user_status = texts.t('ADMIN_NOTIFY_PURCHASE_STATUS_RENEWAL', 'Продление')
            else:
                event_title = texts.t('ADMIN_NOTIFY_PURCHASE_TITLE_PURCHASE', '💎 ПОКУПКА ПОДПИСКИ')
                user_status = texts.t('ADMIN_NOTIFY_PURCHASE_STATUS_FIRST', 'Первая покупка')

            # Получаем название тарифа
            tariff_name = await self._get_tariff_name(db, subscription)

            servers_info = await self._get_servers_info(subscription.connected_squads)
            payment_method = (
                self._get_payment_method_display(transaction.payment_method)
                if transaction
                else texts.t('ADMIN_NOTIFY_PAYMENT_BALANCE', 'Баланс')
            )
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            # Формируем компактное сообщение
            message_lines = [
                f'<b>{event_title}</b>',
                '',
                texts.t('ADMIN_NOTIFY_USER_COMPACT_LINE', '👤 {user} ({identifier})').format(
                    user=user_display, identifier=user_id_display
                ),
            ]

            # Добавляем username только если есть
            username = getattr(user, 'username', None)
            if username:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_USERNAME_COMPACT_LINE', '📱 {username}').format(
                        username=format_username_link(username)
                    )
                )

            message_lines.append(
                texts.t('ADMIN_NOTIFY_STATUS_COMPACT_LINE', '📋 {status}').format(status=user_status)
            )

            # Тариф (если есть)
            if tariff_name:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_TARIFF_COMPACT_LINE', '🏷️ Тариф: <b>{name}</b>').format(
                        name=tariff_name
                    )
                )

            message_lines.extend(
                [
                    '',
                    texts.t('ADMIN_NOTIFY_AMOUNT_METHOD_LINE', '💵 <b>{amount}</b> • {method}').format(
                        amount=settings.format_price(total_amount), method=payment_method
                    ),
                    texts.t('ADMIN_NOTIFY_PERIOD_UNTIL_LINE', '📅 {days} дн. • до {date}').format(
                        days=period_days, date=format_local_datetime(subscription.end_date, '%d.%m.%Y')
                    ),
                    texts.t('ADMIN_NOTIFY_TRAFFIC_DEVICES_LINE', '📊 {traffic} • 📱 {devices} устр.').format(
                        traffic=self._format_traffic(subscription.traffic_limit_gb),
                        devices=subscription.device_limit,
                    ),
                    texts.t('ADMIN_NOTIFY_SERVERS_COMPACT_LINE', '🌐 {servers}').format(servers=servers_info),
                ]
            )

            # Баланс после покупки
            message_lines.append(
                texts.t('ADMIN_NOTIFY_BALANCE_LINE', '💰 Баланс: {amount}').format(
                    amount=settings.format_price(user.balance_kopeks)
                )
            )

            # Реферер (только если есть)
            if user.referred_by_id:
                referrer_info = await self._get_referrer_info(db, user.referred_by_id)
                if referrer_info != 'Нет':
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_REFERRER_COMPACT_LINE', '🔗 Реф: {referrer}').format(
                            referrer=referrer_info
                        )
                    )

            # ID транзакции (только если есть)
            if transaction:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_TRANSACTION_COMPACT_LINE', '🆔 #{transaction_id}').format(
                        transaction_id=transaction.id
                    )
                )

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>',
                ]
            )

            # Маршрутизация по категориям (зеркалит логику заголовков выше)
            if purchase_type == 'renewal' or (
                not was_trial_conversion and purchase_type is None and user.has_had_paid_subscription
            ):
                cat = NotificationCategory.RENEWALS
            else:
                cat = NotificationCategory.PURCHASES

            return await self._send_message('\n'.join(message_lines), category=cat)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о покупке', error=e)
            return False

    async def send_version_update_notification(self, current_version: str, latest_version, total_updates: int) -> bool:
        """Отправляет уведомление о новых обновлениях."""
        if not self._is_enabled():
            return False

        try:
            from app.utils.markdown_to_telegram import github_markdown_to_telegram_html, truncate_for_blockquote

            repo = getattr(settings, 'VERSION_CHECK_REPO', 'fr1ngg/remnawave-bedolaga-telegram-bot')
            release_url = f'https://github.com/{repo}/releases/tag/{latest_version.tag_name}'
            repo_url = f'https://github.com/{repo}'
            timestamp = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')

            texts = get_texts()
            if latest_version.prerelease:
                header = texts.t('ADMIN_NOTIFY_VERSION_PRERELEASE', '🧪 <b>Pre-release</b>')
            elif latest_version.is_dev:
                header = texts.t('ADMIN_NOTIFY_VERSION_DEV_BUILD', '🔧 <b>Dev build</b>')
            else:
                header = texts.t(
                    'ADMIN_NOTIFY_VERSION_UPDATE_AVAILABLE', '🆕 <b>Доступно обновление</b>'
                )

            # -- message prefix (everything before blockquote) --
            prefix_lines = [
                header,
                '',
                f'<code>{current_version}</code>  →  <b><a href="{release_url}">{latest_version.tag_name}</a></b>',
                texts.t('ADMIN_NOTIFY_VERSION_DATE_LINE', '📅 {date}').format(
                    date=latest_version.formatted_date
                ),
                '',
            ]
            message_prefix = '\n'.join(prefix_lines)

            # -- message suffix (everything after blockquote) --
            suffix_lines = ['']
            if total_updates > 1:
                suffix_lines.append(
                    texts.t('ADMIN_NOTIFY_VERSION_TOTAL_UPDATES', 'Доступно обновлений: <b>{count}</b>').format(
                        count=total_updates
                    )
                )
            suffix_lines.extend(
                [
                    texts.t('ADMIN_NOTIFY_VERSION_REPO_LINK', '<a href="{url}">Репозиторий</a>').format(
                        url=repo_url
                    ),
                    '',
                    f'<i>{timestamp}</i>',
                ]
            )
            message_suffix = '\n'.join(suffix_lines)

            # -- description in blockquote --
            raw_description = getattr(latest_version, 'full_description', '') or latest_version.short_description
            description_html = github_markdown_to_telegram_html(raw_description)

            if description_html:
                description_html = truncate_for_blockquote(
                    description_html,
                    message_prefix=message_prefix,
                    message_suffix=message_suffix,
                )
                message = f'{message_prefix}<blockquote expandable>{description_html}</blockquote>{message_suffix}'
            else:
                message = f'{message_prefix}{message_suffix}'

            return await self._send_message(message, category=NotificationCategory.INFRASTRUCTURE)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об обновлении', error=e)
            return False

    async def send_version_check_error_notification(self, error_message: str, current_version: str) -> bool:
        if not self._is_enabled():
            return False

        try:
            message = get_texts().t(
                'ADMIN_NOTIFY_VERSION_CHECK_ERROR',
                """⚠️ <b>ОШИБКА ПРОВЕРКИ ОБНОВЛЕНИЙ</b>

    📦 <b>Текущая версия:</b> <code>{current_version}</code>
    ❌ <b>Ошибка:</b> {error_message}

    🔄 Следующая попытка через час.
    ⚙️ Проверьте доступность GitHub API и настройки сети.

    ⚙️ <i>Система автоматических обновлений • {timestamp}</i>""",
            ).format(
                current_version=current_version,
                error_message=error_message,
                timestamp=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S'),
            )

            return await self._send_message(message, category=NotificationCategory.ERRORS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об ошибке проверки версий', error=e)
            return False

    def _build_balance_topup_message(
        self,
        user: User,
        transaction: Transaction,
        old_balance: int,
        *,
        topup_status: str,
        referrer_info: str,
        subscription: Subscription | None,
        promo_group: PromoGroup | None,
    ) -> str:
        texts = get_texts()
        payment_method = self._get_payment_method_display(transaction.payment_method)
        balance_change = user.balance_kopeks - old_balance
        subscription_status = self._get_subscription_status(subscription)
        timestamp = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
        user_display = self._get_user_display(user)
        user_id_display = self._get_user_identifier_display(user)

        # --- Основной блок ---
        message_lines: list[str] = [
            texts.t('ADMIN_NOTIFY_TOPUP_TITLE', '💰 <b>ПОПОЛНЕНИЕ БАЛАНСА</b>'),
            '',
            texts.t('ADMIN_NOTIFY_USER_COMPACT_LINE', '👤 {user} ({identifier})').format(
                user=user_display, identifier=user_id_display
            ),
        ]

        username = getattr(user, 'username', None)
        if username:
            message_lines.append(
                texts.t('ADMIN_NOTIFY_USERNAME_COMPACT_LINE', '📱 {username}').format(
                    username=format_username_link(username)
                )
            )

        message_lines.append(
            texts.t('ADMIN_NOTIFY_TOPUP_STATUS_LINE', '💳 {status}').format(status=topup_status)
        )

        # Промогруппа -- только название
        if promo_group:
            message_lines.append(
                texts.t('ADMIN_NOTIFY_PROMO_GROUP_PLAIN_LINE', '🏷️ Промогруппа: {name}').format(
                    name=html.escape(promo_group.name)
                )
            )

        message_lines.append('')

        # --- Детали пополнения ---
        message_lines.extend(
            [
                texts.t('ADMIN_NOTIFY_TOPUP_AMOUNT_LINE', '💵 <b>{amount}</b> | {method}').format(
                    amount=settings.format_price(transaction.amount_kopeks), method=payment_method
                ),
                '',
                texts.t(
                    'ADMIN_NOTIFY_TOPUP_BALANCE_CHANGE',
                    '📉 {old_balance} → 📈 {new_balance} (<b>+{change}</b>)',
                ).format(
                    old_balance=settings.format_price(old_balance),
                    new_balance=settings.format_price(user.balance_kopeks),
                    change=settings.format_price(balance_change),
                ),
            ]
        )

        # --- Подписка ---
        message_lines.append(
            texts.t('ADMIN_NOTIFY_SUBSCRIPTION_LINE', '📱 Подписка: {status}').format(
                status=subscription_status
            )
        )

        # --- Реферер (только если есть) ---
        if referrer_info and referrer_info != 'Нет':
            message_lines.append(
                texts.t('ADMIN_NOTIFY_REFERRER_PLAIN_LINE', '🔗 Реферер: {referrer}').format(
                    referrer=referrer_info
                )
            )

        # --- Expandable blockquote с техническими деталями ---
        detail_lines: list[str] = [
            texts.t('ADMIN_NOTIFY_DETAIL_TRANSACTION_ID', 'ID транзакции: {transaction_id}').format(
                transaction_id=transaction.id
            ),
            texts.t('ADMIN_NOTIFY_DETAIL_PAYMENT_METHOD', 'Способ оплаты: {method}').format(
                method=transaction.payment_method or 'balance'
            ),
        ]

        if transaction.external_id:
            detail_lines.append(
                texts.t('ADMIN_NOTIFY_DETAIL_EXTERNAL_ID', 'Внешний ID: {external_id}').format(
                    external_id=transaction.external_id
                )
            )

        if transaction.description:
            desc = transaction.description
            if len(desc) > 120:
                desc = desc[:117] + '...'
            detail_lines.append(
                texts.t('ADMIN_NOTIFY_DETAIL_DESCRIPTION', 'Описание: {description}').format(
                    description=html.escape(desc)
                )
            )

        if transaction.created_at:
            detail_lines.append(
                texts.t('ADMIN_NOTIFY_DETAIL_CREATED_AT', 'Создана: {date}').format(
                    date=format_local_datetime(transaction.created_at, '%d.%m.%Y %H:%M:%S')
                )
            )

        if transaction.completed_at:
            detail_lines.append(
                texts.t('ADMIN_NOTIFY_DETAIL_COMPLETED_AT', 'Завершена: {date}').format(
                    date=format_local_datetime(transaction.completed_at, '%d.%m.%Y %H:%M:%S')
                )
            )

        if transaction.receipt_uuid:
            detail_lines.append(
                texts.t('ADMIN_NOTIFY_DETAIL_RECEIPT_UUID', 'Чек UUID: {uuid}').format(
                    uuid=transaction.receipt_uuid
                )
            )

        blockquote_body = '\n'.join(detail_lines)
        message_lines.extend(
            [
                '',
                f'<blockquote expandable>{blockquote_body}</blockquote>',
            ]
        )

        message_lines.append(f'<i>{timestamp}</i>')

        return '\n'.join(message_lines)

    async def _reload_topup_notification_entities(
        self,
        db: AsyncSession,
        user: User,
        transaction: Transaction,
    ) -> tuple[User, Transaction, Subscription | None, PromoGroup | None]:
        refreshed_user = await get_user_by_id(db, user.id)
        if not refreshed_user:
            raise ValueError(f'Не удалось повторно загрузить пользователя {user.id} для уведомления о пополнении')

        refreshed_transaction = await get_transaction_by_id(db, transaction.id)
        if not refreshed_transaction:
            raise ValueError(f'Не удалось повторно загрузить транзакцию {transaction.id} для уведомления о пополнении')

        subscription = getattr(refreshed_user, 'subscription', None)
        promo_group = await self._get_user_promo_group(db, refreshed_user)

        return refreshed_user, refreshed_transaction, subscription, promo_group

    def _is_lazy_loading_error(self, error: Exception) -> bool:
        message = str(error).lower()
        return (
            isinstance(error, MissingGreenlet)
            or 'greenlet_spawn' in message
            or 'await_only' in message
            or 'missinggreenlet' in message
        )

    async def send_balance_topup_notification(
        self,
        user: User,
        transaction: Transaction,
        old_balance: int,
        *,
        topup_status: str,
        referrer_info: str,
        subscription: Subscription | None,
        promo_group: PromoGroup | None,
        db: AsyncSession | None = None,
    ) -> bool:
        logger.info('Начинаем отправку уведомления о пополнении баланса')

        if db:
            try:
                await self._record_subscription_event(
                    db,
                    event_type='balance_topup',
                    user=user,
                    subscription=subscription,
                    transaction=transaction,
                    amount_kopeks=transaction.amount_kopeks,
                    message='Balance top-up',
                    occurred_at=transaction.completed_at or transaction.created_at,
                    extra={
                        'status': topup_status,
                        'balance_before': old_balance,
                        'balance_after': user.balance_kopeks,
                        'referrer_info': referrer_info,
                        'promo_group_id': getattr(promo_group, 'id', None),
                        'promo_group_name': getattr(promo_group, 'name', None),
                    },
                )
            except Exception:
                logger.error(
                    'Не удалось сохранить событие пополнения баланса пользователя',
                    getattr=getattr(user, 'id', 'unknown'),
                    exc_info=True,
                )

        if not self._is_enabled():
            return False

        try:
            logger.info('Пытаемся создать сообщение уведомления')
            message = self._build_balance_topup_message(
                user,
                transaction,
                old_balance,
                topup_status=topup_status,
                referrer_info=referrer_info,
                subscription=subscription,
                promo_group=promo_group,
            )
            logger.info('Сообщение уведомления создано успешно')
        except Exception as error:
            logger.info(
                'Перехвачена ошибка при создании сообщения уведомления', __name__=type(error).__name__, error=error
            )
            if not self._is_lazy_loading_error(error):
                logger.error('Ошибка подготовки уведомления о пополнении', error=error, exc_info=True)
                return False

            if db is None:
                logger.error(
                    'Недостаточно данных для уведомления о пополнении и отсутствует доступ к БД',
                    error=error,
                    exc_info=True,
                )
                return False

            logger.warning(
                'Повторная загрузка данных для уведомления о пополнении после ошибки ленивой загрузки', error=error
            )

            try:
                logger.info('Пытаемся перезагрузить данные для уведомления')
                (
                    user,
                    transaction,
                    subscription,
                    promo_group,
                ) = await self._reload_topup_notification_entities(db, user, transaction)
                logger.info('Данные успешно перезагружены')
            except Exception as reload_error:
                logger.error(
                    'Ошибка повторной загрузки данных для уведомления о пополнении',
                    reload_error=reload_error,
                    exc_info=True,
                )
                return False

            try:
                logger.info('Пытаемся создать сообщение после перезагрузки данных')
                message = self._build_balance_topup_message(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                )
                logger.info('Сообщение успешно создано после перезагрузки данных')
            except Exception as rebuild_error:
                logger.error(
                    'Ошибка повторной подготовки уведомления о пополнении после повторной загрузки',
                    rebuild_error=rebuild_error,
                    exc_info=True,
                )
                return False

        try:
            return await self._send_message(message, category=NotificationCategory.BALANCE)
        except Exception as e:
            logger.error('Ошибка отправки уведомления о пополнении', error=e, exc_info=True)
            return False

    async def send_subscription_extension_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        transaction: Transaction,
        extended_days: int,
        old_end_date: datetime,
        *,
        new_end_date: datetime | None = None,
        balance_after: int | None = None,
    ) -> bool:
        try:
            current_end_date = new_end_date or subscription.end_date
            current_balance = balance_after if balance_after is not None else user.balance_kopeks

            await self._record_subscription_event(
                db,
                event_type='renewal',
                user=user,
                subscription=subscription,
                transaction=transaction,
                amount_kopeks=abs(transaction.amount_kopeks),
                message='Subscription renewed',
                occurred_at=transaction.completed_at or transaction.created_at,
                extra={
                    'extended_days': extended_days,
                    'previous_end_date': old_end_date.isoformat(),
                    'new_end_date': current_end_date.isoformat(),
                    'payment_method': transaction.payment_method,
                    'balance_after': current_balance,
                },
            )

            if not self._is_enabled():
                return False

            texts = get_texts()
            payment_method = self._get_payment_method_display(transaction.payment_method)
            servers_info = await self._get_servers_info(subscription.connected_squads)
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group)
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message = texts.t(
                'ADMIN_NOTIFY_EXTENSION_MESSAGE',
                """⏰ <b>ПРОДЛЕНИЕ ПОДПИСКИ</b>

👤 <b>Пользователь:</b> {user}
🆔 <b>{label}:</b> {identifier}
📱 <b>Username:</b> {username}

{promo_block}

💰 <b>Платеж:</b>
💵 Сумма: {amount}
💳 Способ: {method}
🆔 ID транзакции: {transaction_id}

📅 <b>Продление:</b>
➕ Добавлено дней: {days}
📆 Было до: {old_end_date}
📆 Стало до: {new_end_date}

📱 <b>Текущие параметры:</b>
📊 Трафик: {traffic}
📱 Устройства: {devices}
🌐 Серверы: {servers}

💰 <b>Баланс после операции:</b> {balance}

⏰ <i>{timestamp}</i>""",
            ).format(
                user=user_display,
                label=user_id_label,
                identifier=user_id_display,
                username=format_username_link(
                    getattr(user, 'username', None),
                    texts.t('ADMIN_NOTIFY_USERNAME_MISSING', 'отсутствует'),
                ),
                promo_block=promo_block,
                amount=settings.format_price(abs(transaction.amount_kopeks)),
                method=payment_method,
                transaction_id=transaction.id,
                days=extended_days,
                old_end_date=format_local_datetime(old_end_date, '%d.%m.%Y %H:%M'),
                new_end_date=format_local_datetime(current_end_date, '%d.%m.%Y %H:%M'),
                traffic=self._format_traffic(subscription.traffic_limit_gb),
                devices=subscription.device_limit,
                servers=servers_info,
                balance=settings.format_price(current_balance),
                timestamp=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S'),
            )

            return await self._send_message(message, category=NotificationCategory.RENEWALS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о продлении', error=e)
            return False

    async def send_promocode_activation_notification(
        self,
        db: AsyncSession,
        user: User,
        promocode_data: dict[str, Any],
        effect_description: str,
        balance_before_kopeks: int | None = None,
        balance_after_kopeks: int | None = None,
    ) -> bool:
        try:
            await self._record_subscription_event(
                db,
                event_type='promocode_activation',
                user=user,
                subscription=None,
                transaction=None,
                amount_kopeks=promocode_data.get('balance_bonus_kopeks'),
                message='Promocode activation',
                occurred_at=datetime.now(UTC),
                extra={
                    'code': promocode_data.get('code'),
                    'type': promocode_data.get('type'),
                    'subscription_days': promocode_data.get('subscription_days'),
                    'balance_bonus_kopeks': promocode_data.get('balance_bonus_kopeks'),
                    'description': effect_description,
                    'valid_until': (
                        promocode_data.get('valid_until').isoformat()
                        if isinstance(promocode_data.get('valid_until'), datetime)
                        else promocode_data.get('valid_until')
                    ),
                    'balance_before_kopeks': balance_before_kopeks,
                    'balance_after_kopeks': balance_after_kopeks,
                },
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие активации промокода пользователя',
                getattr=getattr(user, 'id', 'unknown'),
                exc_info=True,
            )

        if not self._is_enabled():
            return False

        try:
            texts = get_texts()
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group)
            type_display = self._get_promocode_type_display(promocode_data.get('type'))
            usage_info = f'{promocode_data.get("current_uses", 0)}/{promocode_data.get("max_uses", 0)}'
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                texts.t('ADMIN_NOTIFY_PROMOCODE_TITLE', '🎫 <b>АКТИВАЦИЯ ПРОМОКОДА</b>'),
                '',
                texts.t('ADMIN_NOTIFY_USER_LINE', '👤 <b>Пользователь:</b> {user}').format(user=user_display),
                texts.t('ADMIN_NOTIFY_USER_ID_LINE', '🆔 <b>{label}:</b> {value}').format(
                    label=user_id_label, value=user_id_display
                ),
                texts.t('ADMIN_NOTIFY_USERNAME_LINE', '📱 <b>Username:</b> {username}').format(
                    username=format_username_link(
                        getattr(user, 'username', None),
                        texts.t('ADMIN_NOTIFY_USERNAME_MISSING', 'отсутствует'),
                    )
                ),
                '',
                promo_block,
                '',
                texts.t('ADMIN_NOTIFY_PROMOCODE_HEADER', '🎟️ <b>Промокод:</b>'),
                texts.t('ADMIN_NOTIFY_PROMOCODE_CODE', '🔖 Код: <code>{code}</code>').format(
                    code=promocode_data.get('code')
                ),
                texts.t('ADMIN_NOTIFY_PROMOCODE_TYPE', '🧾 Тип: {type}').format(type=type_display),
                texts.t('ADMIN_NOTIFY_PROMOCODE_USES', '📊 Использования: {usage}').format(
                    usage=usage_info
                ),
            ]

            promo_type = promocode_data.get('type')
            balance_bonus = promocode_data.get('balance_bonus_kopeks', 0)
            subscription_days = promocode_data.get('subscription_days', 0)

            if promo_type == PromoCodeType.DISCOUNT.value:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_PROMOCODE_DISCOUNT', '💸 Скидка: {percent}%').format(
                        percent=balance_bonus
                    )
                )
                if subscription_days:
                    message_lines.append(
                        texts.t(
                            'ADMIN_NOTIFY_PROMOCODE_DISCOUNT_HOURS', '⏳ Срок действия скидки: {hours} ч.'
                        ).format(hours=subscription_days)
                    )
                else:
                    message_lines.append(
                        texts.t(
                            'ADMIN_NOTIFY_PROMOCODE_DISCOUNT_FIRST_PURCHASE',
                            '⏳ Срок действия скидки: до первой покупки',
                        )
                    )
            else:
                if balance_bonus:
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_PROMOCODE_BALANCE_BONUS', '💰 Бонус на баланс: {amount}').format(
                            amount=settings.format_price(balance_bonus)
                        )
                    )
                if subscription_days:
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_PROMOCODE_EXTRA_DAYS', '📅 Доп. дни подписки: {days}').format(
                            days=subscription_days
                        )
                    )

            valid_until = promocode_data.get('valid_until')
            if valid_until:
                valid_until_template = texts.t('ADMIN_NOTIFY_PROMOCODE_VALID_UNTIL', '⏳ Действует до: {date}')
                message_lines.append(
                    valid_until_template.format(date=format_local_datetime(valid_until, '%d.%m.%Y %H:%M'))
                    if isinstance(valid_until, datetime)
                    else valid_until_template.format(date=valid_until)
                )

            message_lines.extend(
                [
                    '',
                    texts.t('ADMIN_NOTIFY_BALANCE_HEADER', '💼 <b>Баланс:</b>'),
                    (
                        f'{settings.format_price(balance_before_kopeks)} → {settings.format_price(balance_after_kopeks)}'
                        if balance_before_kopeks is not None and balance_after_kopeks is not None
                        else texts.t('ADMIN_NOTIFY_BALANCE_UNCHANGED', 'ℹ️ Баланс не изменился')
                    ),
                    '',
                    texts.t('ADMIN_NOTIFY_EFFECT_HEADER', '📝 <b>Эффект:</b>'),
                    effect_description.strip()
                    or texts.t('ADMIN_NOTIFY_PROMOCODE_ACTIVATED', '✅ Промокод активирован'),
                    '',
                    texts.t('ADMIN_NOTIFY_TIMESTAMP_LINE', '⏰ <i>{timestamp}</i>').format(
                        timestamp=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
                    ),
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об активации промокода', error=e)
            return False

    async def send_campaign_link_visit_notification(
        self,
        db: AsyncSession,
        telegram_user: types.User,
        campaign: AdvertisingCampaign,
        user: User | None = None,
    ) -> bool:
        # Дедуп: если юзер уже зарегистрирован в этой кампании
        # (AdvertisingCampaignRegistration.UniqueConstraint(campaign_id, user_id))
        # — повторный /start не должен слать новое уведомление в админ-чат, иначе
        # кол-во сообщений в чате превышает реальное число регистраций в БД и
        # вводит админа в заблуждение. Для новых юзеров (user is None) уведомление
        # уходит как раньше — это первичный переход.
        if user:
            existing_registration = await db.execute(
                select(AdvertisingCampaignRegistration.id).where(
                    AdvertisingCampaignRegistration.campaign_id == campaign.id,
                    AdvertisingCampaignRegistration.user_id == user.id,
                )
            )
            if existing_registration.scalar_one_or_none() is not None:
                logger.debug(
                    'Skip campaign visit notification: user already registered in campaign',
                    user_id=user.id,
                    campaign_id=campaign.id,
                )
                return False

            try:
                await self._record_subscription_event(
                    db,
                    event_type='referral_link_visit',
                    user=user,
                    subscription=None,
                    transaction=None,
                    amount_kopeks=None,
                    message='Referral link visit',
                    occurred_at=datetime.now(UTC),
                    extra={
                        'campaign_id': campaign.id,
                        'campaign_name': campaign.name,
                        'start_parameter': campaign.start_parameter,
                        'was_registered': bool(user),
                    },
                )
            except Exception:
                logger.error(
                    'Не удалось сохранить событие перехода по кампании для пользователя',
                    getattr=getattr(user, 'id', 'unknown'),
                    exc_info=True,
                )

        if not self._is_enabled():
            return False

        try:
            texts = get_texts()
            full_name = telegram_user.full_name or telegram_user.username or str(telegram_user.id)
            user_status = (
                texts.t('ADMIN_NOTIFY_USER_STATUS_NEW', '🆕 Новый')
                if not user
                else texts.t('ADMIN_NOTIFY_CAMPAIGN_USER_STATUS_EXISTING', '👥 Существующий')
            )

            message_lines = [
                texts.t('ADMIN_NOTIFY_CAMPAIGN_VISIT_TITLE', '📣 <b>ПЕРЕХОД ПО РК</b>'),
                '',
                texts.t('ADMIN_NOTIFY_CAMPAIGN_LINE', '🧾 {name} (<code>{parameter}</code>)').format(
                    name=html.escape(campaign.name), parameter=html.escape(campaign.start_parameter)
                ),
                '',
                texts.t('ADMIN_NOTIFY_CAMPAIGN_USER_LINE', '👤 {user} (<code>{identifier}</code>)').format(
                    user=html.escape(full_name), identifier=telegram_user.id
                ),
            ]

            if telegram_user.username:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_USERNAME_COMPACT_LINE', '📱 {username}').format(
                        username=format_username_link(telegram_user.username)
                    )
                )

            message_lines.append(
                texts.t('ADMIN_NOTIFY_STATUS_COMPACT_LINE', '📋 {status}').format(status=user_status)
            )

            # Промогруппа — только если есть
            if user:
                promo_group = await self._get_user_promo_group(db, user)
                if promo_group:
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_PROMO_GROUP_PLAIN_LINE', '🏷️ Промогруппа: {name}').format(
                            name=html.escape(promo_group.name)
                        )
                    )

            message_lines.append('')

            # Загружаем название тарифа для tariff-бонуса
            tariff_name = None
            if campaign.is_tariff_bonus and campaign.tariff_id:
                try:
                    from app.database.crud.tariff import get_tariff_by_id

                    tariff = await get_tariff_by_id(db, campaign.tariff_id)
                    if tariff:
                        tariff_name = html.escape(tariff.name)
                except Exception:
                    pass

            # Бонус кампании
            bonus_lines = self._format_campaign_bonus(campaign, tariff_name=tariff_name)
            message_lines.extend(bonus_lines)

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о переходе по кампании', error=e)
            return False

    async def send_campaign_registration_notification(
        self,
        db: AsyncSession,
        telegram_user_id: int,
        telegram_user_name: str,
        telegram_username: str | None,
        campaign: AdvertisingCampaign,
        user: User,
        *,
        bonus_type: str,
        balance_kopeks: int = 0,
        subscription_days: int | None = None,
        subscription_traffic_gb: int | None = None,
        subscription_device_limit: int | None = None,
        tariff_name: str | None = None,
    ) -> bool:
        """Уведомление о СОВЕРШЁННОЙ регистрации по рекламной кампании.

        Шлётся ровно один раз на каждую новую запись в advertising_campaign_registrations
        (caller передаёт is_new_registration=True). Это даёт паритет: число сообщений
        в админ-чате равно числу регистраций в кабинете.
        """
        if not self._is_enabled():
            return False

        try:
            await self._record_subscription_event(
                db,
                event_type='campaign_registration',
                user=user,
                subscription=None,
                transaction=None,
                amount_kopeks=balance_kopeks or None,
                message='Campaign registration completed',
                occurred_at=datetime.now(UTC),
                extra={
                    'campaign_id': campaign.id,
                    'campaign_name': campaign.name,
                    'start_parameter': campaign.start_parameter,
                    'bonus_type': bonus_type,
                },
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие регистрации по кампании',
                user_id=user.id,
                campaign_id=campaign.id,
                exc_info=True,
            )

        try:
            texts = get_texts()
            message_lines = [
                texts.t('ADMIN_NOTIFY_CAMPAIGN_REGISTRATION_TITLE', '✅ <b>РЕГИСТРАЦИЯ ПО РК</b>'),
                '',
                texts.t('ADMIN_NOTIFY_CAMPAIGN_LINE', '🧾 {name} (<code>{parameter}</code>)').format(
                    name=html.escape(campaign.name), parameter=html.escape(campaign.start_parameter)
                ),
                '',
                texts.t('ADMIN_NOTIFY_CAMPAIGN_USER_LINE', '👤 {user} (<code>{identifier}</code>)').format(
                    user=html.escape(telegram_user_name), identifier=telegram_user_id
                ),
            ]
            if telegram_username:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_USERNAME_COMPACT_LINE', '📱 {username}').format(
                        username=format_username_link(telegram_username)
                    )
                )

            promo_group = await self._get_user_promo_group(db, user)
            if promo_group:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_PROMO_GROUP_PLAIN_LINE', '🏷️ Промогруппа: {name}').format(
                        name=html.escape(promo_group.name)
                    )
                )

            message_lines.append('')

            bonus_lines = self._format_campaign_bonus(campaign, tariff_name=tariff_name)
            message_lines.extend(bonus_lines)

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error(
                'Ошибка отправки уведомления о регистрации по кампании',
                error=str(e),
                user_id=user.id,
                campaign_id=campaign.id,
                exc_info=True,
            )
            return False

    async def send_user_promo_group_change_notification(
        self,
        db: AsyncSession,
        user: User,
        old_group: PromoGroup | None,
        new_group: PromoGroup,
        *,
        reason: str | None = None,
        initiator: User | None = None,
        automatic: bool = False,
    ) -> bool:
        try:
            await self._record_subscription_event(
                db,
                event_type='promo_group_change',
                user=user,
                subscription=None,
                transaction=None,
                message='Promo group change',
                occurred_at=datetime.now(UTC),
                extra={
                    'old_group_id': getattr(old_group, 'id', None),
                    'old_group_name': getattr(old_group, 'name', None),
                    'new_group_id': new_group.id,
                    'new_group_name': new_group.name,
                    'reason': reason,
                    'initiator_id': getattr(initiator, 'id', None),
                    'initiator_telegram_id': getattr(initiator, 'telegram_id', None),
                    'automatic': automatic,
                },
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие смены промогруппы пользователя',
                getattr=getattr(user, 'id', 'unknown'),
                exc_info=True,
            )

        if not self._is_enabled():
            return False

        try:
            texts = get_texts()
            title = (
                texts.t('ADMIN_NOTIFY_PROMO_GROUP_CHANGE_TITLE_AUTO', '🤖 АВТОМАТИЧЕСКАЯ СМЕНА ПРОМОГРУППЫ')
                if automatic
                else texts.t('ADMIN_NOTIFY_PROMO_GROUP_CHANGE_TITLE', '👥 СМЕНА ПРОМОГРУППЫ')
            )
            initiator_line = None
            if initiator:
                initiator_line = texts.t(
                    'ADMIN_NOTIFY_INITIATOR_LINE', '👮 <b>Инициатор:</b> {name} (ID: {telegram_id})'
                ).format(name=html.escape(initiator.full_name), telegram_id=initiator.telegram_id)
            elif automatic:
                initiator_line = texts.t(
                    'ADMIN_NOTIFY_INITIATOR_AUTOMATIC', '🤖 Автоматическое назначение'
                )
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                f'{title}',
                '',
                texts.t('ADMIN_NOTIFY_USER_LINE', '👤 <b>Пользователь:</b> {user}').format(user=user_display),
                texts.t('ADMIN_NOTIFY_USER_ID_LINE', '🆔 <b>{label}:</b> {value}').format(
                    label=user_id_label, value=user_id_display
                ),
                texts.t('ADMIN_NOTIFY_USERNAME_LINE', '📱 <b>Username:</b> {username}').format(
                    username=format_username_link(
                        getattr(user, 'username', None),
                        texts.t('ADMIN_NOTIFY_USERNAME_MISSING', 'отсутствует'),
                    )
                ),
                '',
                self._format_promo_group_block(
                    new_group,
                    title=texts.t('ADMIN_NOTIFY_PROMO_GROUP_NEW_TITLE', 'Новая промогруппа'),
                    icon='🏆',
                ),
            ]

            if old_group and old_group.id != new_group.id:
                message_lines.extend(
                    [
                        '',
                        self._format_promo_group_block(
                            old_group,
                            title=texts.t('ADMIN_NOTIFY_PROMO_GROUP_OLD_TITLE', 'Предыдущая промогруппа'),
                            icon='♻️',
                        ),
                    ]
                )

            if initiator_line:
                message_lines.extend(['', initiator_line])

            if reason:
                message_lines.extend(
                    [
                        '',
                        texts.t('ADMIN_NOTIFY_REASON_LINE', '📝 Причина: {reason}').format(reason=reason),
                    ]
                )

            message_lines.extend(
                [
                    '',
                    texts.t('ADMIN_NOTIFY_USER_BALANCE_LINE', '💰 Баланс пользователя: {amount}').format(
                        amount=settings.format_price(user.balance_kopeks)
                    ),
                    texts.t('ADMIN_NOTIFY_TIMESTAMP_LINE', '⏰ <i>{timestamp}</i>').format(
                        timestamp=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
                    ),
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о смене промогруппы', error=e)
            return False

    def _resolve_topic_id(self, category: NotificationCategory | None = None) -> int | None:
        """Определяет topic_id для сообщения.

        Если указана category и для неё настроен топик — возвращает его.
        Иначе — fallback на self.topic_id (общий топик).
        """
        if category:
            topic = self.category_topics.get(category)
            if topic is not None:
                return topic
        return self.topic_id

    def resolve_recipient_role(self) -> str:
        """Определяет роль получателя уведомления по chat_id.

        В личном чате chat_id совпадает с telegram_id получателя, что позволяет
        проверить права до отправки без I/O (данные читаются из памяти).

        Returns:
            'admin'     — полный набор кнопок;
            'moderator' — набор без «👤 К пользователю» (@admin_required);
            'group'     — групповой/супергруппа/канал админ-чат: только надёжные
                          (не-FSM) кнопки, т.к. конкретного получателя не определить
                          и FSM-ввод в общем чате не работает (privacy mode бота);
            'none'      — без кнопок (посторонний в личке, либо
                          ADMIN_NOTIFICATIONS_CHAT_ID задан строкой @username).
        """
        try:
            chat_id = int(self.chat_id)
        except (TypeError, ValueError):
            return 'none'  # строка @username или None — тип чата не определить
        if chat_id < 0:
            # супергруппа / канал / старая группа — доверенный админ-чат оператора,
            # но конкретного получателя не определить → только надёжные кнопки.
            return 'group'
        if chat_id == 0:
            return 'none'  # невалидный chat_id
        if settings.is_admin(chat_id):
            return 'admin'

        from app.services.support_settings_service import SupportSettingsService

        if SupportSettingsService.is_moderator(chat_id):
            return 'moderator'

        return 'none'  # личка постороннего — не показываем кнопки

    async def _send_message(
        self,
        text: str,
        reply_markup: types.InlineKeyboardMarkup | None = None,
        *,
        category: NotificationCategory | None = None,
    ) -> bool:
        if not self._is_enabled():
            return False

        # Per-category suppression
        if category and not self.category_enabled.get(category, True):
            logger.debug('Уведомление подавлено (категория отключена)', category=category.value)
            return False

        thread_id = self._resolve_topic_id(category)

        # Rich-вид (Bot API 10.1): заголовок, разделители, footer с tg-time.
        # При недоступности/ошибке молча продолжаем классическим путём ниже
        # (там ретраи и обработка flood control).
        try:
            rich_html = classic_admin_html_to_rich(text)
            if await try_send_rich_admin_message(
                self.bot, self.chat_id, rich_html, thread_id=thread_id, reply_markup=reply_markup
            ):
                logger.info('Rich-уведомление отправлено в чат', chat_id=self.chat_id, category=category)
                return True
        except Exception as rich_error:
            logger.warning('Сбой rich-рендера админ-уведомления', error=str(rich_error))

        message_kwargs: dict[str, Any] = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }
        if thread_id:
            message_kwargs['message_thread_id'] = thread_id
        if reply_markup is not None:
            message_kwargs['reply_markup'] = reply_markup

        # ВАЖНО: вся ветка ошибок ниже логируется через logger.warning, а не
        # logger.error. Иначе TelegramNotifierProcessor попытается переслать
        # ошибку в этот же админ-чат, упрётся в тот же flood control — петля
        # усиления (баг с node.connection_lost/restored, 7-8 webhook'ов подряд).
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                await self.bot.send_message(**message_kwargs)
                logger.info('Уведомление отправлено в чат', chat_id=self.chat_id, category=category)
                return True

            except TelegramForbiddenError:
                logger.warning('Бот не имеет прав для отправки в чат', chat_id=self.chat_id)
                return False

            except TelegramBadRequest as e:
                logger.warning(
                    'Ошибка отправки уведомления в админ-чат',
                    error=_redact_telegram_secrets(str(e))[:200],
                )
                return False

            except TelegramRetryAfter as e:
                # Flood control: ждём столько, сколько сказал Telegram (cap 30s),
                # потом ретраим. До фикса исключение проваливалось в bare
                # except → logger.error → петля через TelegramNotifierProcessor.
                requested_retry_after = max(1, int(getattr(e, 'retry_after', 1)))
                retry_after = min(requested_retry_after, 30)
                log_kwargs: dict[str, Any] = {
                    'chat_id': self.chat_id,
                    'retry_after': retry_after,
                    'attempt': attempt,
                }
                if requested_retry_after > retry_after:
                    # Telegram реально просит дольше cap'а — видимый сигнал,
                    # что бот аккаунт перегружен сильнее обычного flood-control'а.
                    log_kwargs['retry_after_requested'] = requested_retry_after
                    log_kwargs['clamped'] = True
                logger.warning('Telegram flood control при отправке в админ-чат', **log_kwargs)
                if attempt < max_attempts:
                    await asyncio.sleep(retry_after)
                    continue
                return False

            except (TelegramNetworkError, TelegramServerError) as e:
                # Транзиентные сетевые/5xx — warning, не error.
                logger.warning(
                    'Транзиентная сетевая ошибка отправки в админ-чат',
                    chat_id=self.chat_id,
                    error=_redact_telegram_secrets(str(e))[:200],
                    error_type=type(e).__name__,
                    attempt=attempt,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
                    continue
                return False

            except Exception as e:
                logger.warning(
                    'Неожиданная ошибка при отправке в админ-чат',
                    chat_id=self.chat_id,
                    error=_redact_telegram_secrets(str(e))[:200],
                    error_type=type(e).__name__,
                )
                return False

        return False

    def _is_enabled(self) -> bool:
        return self.enabled and bool(self.chat_id)

    @property
    def is_enabled(self) -> bool:
        """Public check for whether admin notifications are configured and active."""
        return self._is_enabled()

    async def send_admin_notification(
        self,
        text: str,
        reply_markup: types.InlineKeyboardMarkup | None = None,
        *,
        category: NotificationCategory | None = None,
    ) -> bool:
        """Send a generic notification to admin chat with optional inline keyboard."""
        if not self._is_enabled():
            return False
        return await self._send_message(text, reply_markup=reply_markup, category=category)

    async def send_guest_purchase_notification(
        self,
        purchase: GuestPurchase,
        tariff_name: str,
        *,
        is_pending_activation: bool = False,
    ) -> bool:
        """Send admin notification for a guest/gift purchase (landing or cabinet)."""
        if not self._is_enabled():
            return False

        try:
            texts = get_texts()
            is_cabinet = purchase.source == 'cabinet'

            # Event title
            if is_cabinet and purchase.is_gift:
                event_title = texts.t('ADMIN_NOTIFY_GUEST_TITLE_CABINET_GIFT', '🎁 ПОДАРОК ИЗ КАБИНЕТА')
            elif is_pending_activation:
                event_title = texts.t(
                    'ADMIN_NOTIFY_GUEST_TITLE_PENDING', '⏳ ПОКУПКА С ЛЕНДИНГА (ожидает активации)'
                )
            elif purchase.is_gift:
                event_title = texts.t(
                    'ADMIN_NOTIFY_GUEST_TITLE_GIFT', '🎁 ПОКУПКА В ПОДАРОК С ЛЕНДИНГА'
                )
            else:
                event_title = texts.t('ADMIN_NOTIFY_GUEST_TITLE_LANDING', '🛒 ПОКУПКА С ЛЕНДИНГА')

            # Contact info
            contact_display = html.escape(purchase.contact_value or '—')
            contact_icon = '📧' if purchase.contact_type == 'email' else '📱'

            payment_method = self._get_payment_method_display(purchase.payment_method)

            message_lines = [
                f'<b>{event_title}</b>',
                '',
            ]

            if is_cabinet:
                # Cabinet gift: show buyer with link to user profile
                buyer = getattr(purchase, 'buyer', None)
                if buyer:
                    if buyer.username:
                        buyer_display = format_username_link(buyer.username)
                    else:
                        buyer_name = buyer.email or f'id:{buyer.id}'
                        buyer_display = f'<code>{html.escape(buyer_name)}</code>'
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_GUEST_BUYER_LINE', '👤 Покупатель: {buyer}').format(
                            buyer=buyer_display
                        )
                    )
                else:
                    message_lines.append(
                        texts.t(
                            'ADMIN_NOTIFY_GUEST_BUYER_CONTACT_LINE', '{icon} Покупатель: <code>{contact}</code>'
                        ).format(icon=contact_icon, contact=contact_display)
                    )
            else:
                # Landing: show page slug and buyer contact
                landing_slug = '—'
                try:
                    landing = purchase.landing
                    if landing:
                        landing_slug = landing.slug
                    elif purchase.landing_id:
                        landing_slug = f'ID:{purchase.landing_id}'
                except Exception:
                    if purchase.landing_id:
                        landing_slug = f'ID:{purchase.landing_id}'
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_GUEST_PAGE_LINE', '🌐 Страница: <b>/buy/{slug}</b>').format(
                        slug=html.escape(landing_slug)
                    )
                )
                message_lines.append(
                    texts.t(
                        'ADMIN_NOTIFY_GUEST_BUYER_CONTACT_LINE', '{icon} Покупатель: <code>{contact}</code>'
                    ).format(icon=contact_icon, contact=contact_display)
                )

            if purchase.is_gift:
                if purchase.gift_recipient_value:
                    recipient_icon = '📧' if purchase.gift_recipient_type == 'email' else '📱'
                    recipient_value = html.escape(purchase.gift_recipient_value)
                    message_lines.append(
                        texts.t(
                            'ADMIN_NOTIFY_GUEST_RECIPIENT_LINE', '{icon} Получатель: <code>{contact}</code>'
                        ).format(icon=recipient_icon, contact=recipient_value)
                    )
                else:
                    message_lines.append(
                        texts.t(
                            'ADMIN_NOTIFY_GUEST_RECIPIENT_BY_CODE', '🔗 Получатель: <i>по коду активации</i>'
                        )
                    )
                if purchase.gift_message:
                    raw_msg = purchase.gift_message[:100]
                    suffix = '…' if len(purchase.gift_message) > 100 else ''
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_GUEST_GIFT_MESSAGE', '💬 <i>{message}{suffix}</i>').format(
                            message=html.escape(raw_msg), suffix=suffix
                        )
                    )

            # Payment details in blockquote
            payment_lines = [
                '<blockquote>',
                texts.t('ADMIN_NOTIFY_TARIFF_COMPACT_LINE', '🏷️ Тариф: <b>{name}</b>').format(
                    name=html.escape(tariff_name)
                ),
                texts.t('ADMIN_NOTIFY_PERIOD_SHORT_LINE', '📅 Период: {days} дн.').format(
                    days=purchase.period_days
                ),
                texts.t('ADMIN_NOTIFY_AMOUNT_METHOD_LINE', '💵 <b>{amount}</b> • {method}').format(
                    amount=settings.format_price(purchase.amount_kopeks), method=payment_method
                ),
            ]

            if purchase.payment_id:
                payment_lines.append(f'🆔 {html.escape(str(purchase.payment_id))}')

            payment_lines.append('</blockquote>')
            message_lines.extend(payment_lines)

            message_lines.append(f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>')

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PURCHASES)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о гостевой покупке', error=e)
            return False

    async def send_webhook_notification(self, text: str) -> bool:
        """Send a generic webhook/infrastructure notification to admin chat.

        Used by RemnaWaveWebhookService for node, service, and CRM events.
        The caller is responsible for HTML-escaping all untrusted data in `text`.
        """
        if not self._is_enabled():
            return False
        return await self._send_message(text, category=NotificationCategory.INFRASTRUCTURE)

    def _get_payment_method_display(self, payment_method: str | None) -> str:
        texts = get_texts()
        if not payment_method:
            return texts.t('ADMIN_NOTIFY_PAYMENT_FROM_BALANCE', '💰 С баланса')

        card_template = texts.t('ADMIN_NOTIFY_PAYMENT_CARD', '💳 {name} (карта)')
        crypto_template = texts.t('ADMIN_NOTIFY_PAYMENT_CRYPTO', '🪙 {name} (крипто)')
        provider_template = texts.t('ADMIN_NOTIFY_PAYMENT_PROVIDER', '💳 {name}')

        method_names: dict[str, str] = {
            'telegram_stars': texts.t('ADMIN_NOTIFY_PAYMENT_TELEGRAM_STARS', '⭐ Telegram Stars'),
            'yookassa': texts.t('ADMIN_NOTIFY_PAYMENT_YOOKASSA', '💳 YooKassa (карта)'),
            'tribute': texts.t('ADMIN_NOTIFY_PAYMENT_TRIBUTE', '💎 Tribute (карта)'),
            'mulenpay': card_template.format(name=settings.get_mulenpay_display_name()),
            'pal24': texts.t('ADMIN_NOTIFY_PAYMENT_SBP', '🏦 {name} (СБП)').format(
                name=settings.get_pal24_display_name()
            ),
            'cryptobot': crypto_template.format(name=settings.get_cryptobot_display_name()),
            'heleket': crypto_template.format(name=settings.get_heleket_display_name()),
            'wata': provider_template.format(name=settings.get_wata_display_name()),
            'platega': provider_template.format(name=settings.get_platega_display_name()),
            'cloudpayments': provider_template.format(name=settings.get_cloudpayments_display_name()),
            'freekassa': provider_template.format(name=settings.get_freekassa_display_name()),
            'kassa_ai': provider_template.format(name=settings.get_kassa_ai_display_name()),
            'cispay': provider_template.format(name=settings.get_cispay_display_name()),
            'manual': texts.t('ADMIN_NOTIFY_PAYMENT_MANUAL', '🛠️ Вручную (админ)'),
            'balance': texts.t('ADMIN_NOTIFY_PAYMENT_FROM_BALANCE', '💰 С баланса'),
        }

        return method_names.get(payment_method, provider_template.format(name=html.escape(payment_method)))

    def _format_traffic(self, traffic_gb: int) -> str:
        texts = get_texts()
        if traffic_gb == 0:
            return texts.t('ADMIN_NOTIFY_TRAFFIC_UNLIMITED', '∞ Безлимит')
        return texts.t('ADMIN_NOTIFY_TRAFFIC_GB', '{traffic} ГБ').format(traffic=traffic_gb)

    def _get_subscription_status(self, subscription: Subscription | None) -> str:
        texts = get_texts()
        if not subscription:
            return texts.t('ADMIN_NOTIFY_SUB_STATUS_NONE', '❌ Нет подписки')

        if subscription.is_trial:
            return texts.t('ADMIN_NOTIFY_SUB_STATUS_TRIAL', '🎯 Триал (до {date})').format(
                date=format_local_datetime(subscription.end_date, '%d.%m')
            )
        if subscription.is_active:
            return texts.t('ADMIN_NOTIFY_SUB_STATUS_ACTIVE', '✅ Активна (до {date})').format(
                date=format_local_datetime(subscription.end_date, '%d.%m')
            )
        return texts.t('ADMIN_USER_SUBSCRIPTION_STATUS_INACTIVE', '❌ Неактивна')

    async def _get_servers_info(self, squad_uuids: list) -> str:
        texts = get_texts()
        if not squad_uuids:
            return texts.t('ADMIN_NOTIFY_SERVERS_NONE', '❌ Нет серверов')

        try:
            from app.handlers.subscription import get_servers_display_names

            servers_names = await get_servers_display_names(squad_uuids)
            return texts.t('ADMIN_NOTIFY_SERVERS_COUNT_NAMES', '{count} шт. ({names})').format(
                count=len(squad_uuids), names=servers_names
            )
        except Exception as e:
            logger.warning('Не удалось получить названия серверов', error=e)
            return texts.t('ADMIN_NOTIFY_SERVERS_COUNT', '{count} шт.').format(count=len(squad_uuids))

    async def send_maintenance_status_notification(
        self, event_type: str, status: str, details: dict[str, Any] = None
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            texts = get_texts()
            details = details or {}

            if event_type == 'enable':
                if details.get('auto_enabled', False):
                    icon = '⚠️'
                    title = texts.t(
                        'ADMIN_NOTIFY_MAINTENANCE_TITLE_AUTO_ENABLE', 'АВТОМАТИЧЕСКОЕ ВКЛЮЧЕНИЕ ТЕХРАБОТ'
                    )
                else:
                    icon = '🔧'
                    title = texts.t('ADMIN_NOTIFY_MAINTENANCE_TITLE_ENABLE', 'ВКЛЮЧЕНИЕ ТЕХРАБОТ')

            elif event_type == 'disable':
                icon = '✅'
                title = texts.t('ADMIN_NOTIFY_MAINTENANCE_TITLE_DISABLE', 'ОТКЛЮЧЕНИЕ ТЕХРАБОТ')

            elif event_type == 'api_status':
                if status == 'online':
                    icon = '🟢'
                    title = texts.t('ADMIN_NOTIFY_MAINTENANCE_TITLE_API_ONLINE', 'API REMNAWAVE ВОССТАНОВЛЕНО')
                else:
                    icon = '🔴'
                    title = texts.t('ADMIN_NOTIFY_MAINTENANCE_TITLE_API_OFFLINE', 'API REMNAWAVE НЕДОСТУПНО')

            elif event_type == 'monitoring':
                if status == 'started':
                    icon = '🔍'
                    title = texts.t('ADMIN_NOTIFY_MAINTENANCE_TITLE_MONITORING_ON', 'МОНИТОРИНГ ЗАПУЩЕН')
                else:
                    icon = '⏹️'
                    title = texts.t('ADMIN_NOTIFY_MAINTENANCE_TITLE_MONITORING_OFF', 'МОНИТОРИНГ ОСТАНОВЛЕН')
            else:
                icon = 'ℹ️'
                title = texts.t('ADMIN_NOTIFY_MAINTENANCE_TITLE_DEFAULT', 'СИСТЕМА ТЕХРАБОТ')

            message_parts = [f'{icon} <b>{title}</b>', '']

            if event_type == 'enable':
                if details.get('reason'):
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_REASON_BLOCK_LINE', '📋 <b>Причина:</b> {reason}').format(
                            reason=details['reason']
                        )
                    )

                if details.get('enabled_at'):
                    enabled_at = details['enabled_at']
                    if isinstance(enabled_at, str):
                        enabled_at = datetime.fromisoformat(enabled_at)
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_MAINTENANCE_ENABLED_AT', '🕐 <b>Время включения:</b> {date}').format(
                            date=format_local_datetime(enabled_at, '%d.%m.%Y %H:%M:%S')
                        )
                    )

                message_parts.append(
                    texts.t('ADMIN_NOTIFY_MAINTENANCE_AUTO', '🤖 <b>Автоматически:</b> {value}').format(
                        value=(
                            texts.t('ADMIN_NOTIFY_YES', 'Да')
                            if details.get('auto_enabled', False)
                            else texts.t('ADMIN_NOTIFY_NO', 'Нет')
                        )
                    )
                )
                message_parts.append('')
                message_parts.append(
                    texts.t(
                        'ADMIN_NOTIFY_MAINTENANCE_USERS_BLOCKED',
                        '❗ Обычные пользователи временно не могут использовать бота.',
                    )
                )

            elif event_type == 'disable':
                if details.get('disabled_at'):
                    disabled_at = details['disabled_at']
                    if isinstance(disabled_at, str):
                        disabled_at = datetime.fromisoformat(disabled_at)
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_MAINTENANCE_DISABLED_AT', '🕐 <b>Время отключения:</b> {date}').format(
                            date=format_local_datetime(disabled_at, '%d.%m.%Y %H:%M:%S')
                        )
                    )

                if details.get('duration'):
                    duration = details['duration']
                    if isinstance(duration, (int, float)):
                        hours = int(duration // 3600)
                        minutes = int((duration % 3600) // 60)
                        if hours > 0:
                            duration_str = texts.t(
                                'ADMIN_NOTIFY_DURATION_HOURS_MINUTES', '{hours}ч {minutes}мин'
                            ).format(hours=hours, minutes=minutes)
                        else:
                            duration_str = texts.t('ADMIN_NOTIFY_DURATION_MINUTES', '{minutes}мин').format(
                                minutes=minutes
                            )
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_DURATION_LINE', '⏱️ <b>Длительность:</b> {duration}').format(
                                duration=duration_str
                            )
                        )

                message_parts.append(
                    texts.t('ADMIN_NOTIFY_MAINTENANCE_WAS_AUTO', '🤖 <b>Было автоматическим:</b> {value}').format(
                        value=(
                            texts.t('ADMIN_NOTIFY_YES', 'Да')
                            if details.get('was_auto', False)
                            else texts.t('ADMIN_NOTIFY_NO', 'Нет')
                        )
                    )
                )
                message_parts.append('')
                message_parts.append(
                    texts.t('ADMIN_NOTIFY_MAINTENANCE_SERVICE_AVAILABLE', '✅ Сервис снова доступен для пользователей.')
                )

            elif event_type == 'api_status':
                message_parts.append(
                    texts.t('ADMIN_NOTIFY_API_URL_LINE', '🔗 <b>API URL:</b> {url}').format(
                        url=details.get('api_url', texts.t('ADMIN_NOTIFY_UNKNOWN', 'неизвестно'))
                    )
                )

                if status == 'online':
                    if details.get('response_time'):
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_RESPONSE_TIME_LINE', '⚡ <b>Время отклика:</b> {value} сек').format(
                                value=details['response_time']
                            )
                        )

                    if details.get('consecutive_failures', 0) > 0:
                        message_parts.append(
                            texts.t(
                                'ADMIN_NOTIFY_FAILURES_WERE_LINE', '🔄 <b>Неудачных попыток было:</b> {count}'
                            ).format(count=details['consecutive_failures'])
                        )

                    message_parts.append('')
                    message_parts.append(texts.t('ADMIN_NOTIFY_API_RESPONDING', 'API снова отвечает на запросы.'))

                else:
                    if details.get('consecutive_failures'):
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_ATTEMPT_NUMBER_LINE', '🔄 <b>Попытка №:</b> {count}').format(
                                count=details['consecutive_failures']
                            )
                        )

                    if details.get('error'):
                        error_msg = str(details['error'])[:100]
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_ERROR_LINE', '❌ <b>Ошибка:</b> {error}').format(error=error_msg)
                        )

                    message_parts.append('')
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_API_FAILURES_STARTED', '⚠️ Началась серия неудачных проверок API.')
                    )

            elif event_type == 'monitoring':
                if status == 'started':
                    if details.get('check_interval'):
                        message_parts.append(
                            texts.t(
                                'ADMIN_NOTIFY_CHECK_INTERVAL_LINE', '🔄 <b>Интервал проверки:</b> {value} сек'
                            ).format(value=details['check_interval'])
                        )

                    if details.get('auto_enable_configured') is not None:
                        auto_enable = (
                            texts.t('ADMIN_NOTIFY_ENABLED', 'Включено')
                            if details['auto_enable_configured']
                            else texts.t('ADMIN_NOTIFY_DISABLED', 'Отключено')
                        )
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_AUTO_ENABLE_LINE', '🤖 <b>Автовключение:</b> {value}').format(
                                value=auto_enable
                            )
                        )

                    if details.get('max_failures'):
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_MAX_FAILURES_LINE', '🎯 <b>Порог ошибок:</b> {count}').format(
                                count=details['max_failures']
                            )
                        )

                    message_parts.append('')
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_MONITORING_WATCHING', 'Система будет следить за доступностью API.')
                    )

                else:
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_MONITORING_STOPPED', 'Автоматический мониторинг API остановлен.')
                    )

            message_parts.append('')
            message_parts.append(
                texts.t('ADMIN_NOTIFY_TIMESTAMP_LINE', '⏰ <i>{timestamp}</i>').format(
                    timestamp=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
                )
            )

            message = '\n'.join(message_parts)

            return await self._send_message(message, category=NotificationCategory.INFRASTRUCTURE)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о техработах', error=e)
            return False

    async def send_remnawave_panel_status_notification(self, status: str, details: dict[str, Any] = None) -> bool:
        if not self._is_enabled():
            return False

        try:
            texts = get_texts()
            details = details or {}

            status_config = {
                'online': {
                    'icon': '🟢',
                    'title': texts.t('ADMIN_NOTIFY_PANEL_TITLE_ONLINE', 'ПАНЕЛЬ REMNAWAVE ДОСТУПНА'),
                    'alert_type': 'success',
                },
                'offline': {
                    'icon': '🔴',
                    'title': texts.t('ADMIN_NOTIFY_PANEL_TITLE_OFFLINE', 'ПАНЕЛЬ REMNAWAVE НЕДОСТУПНА'),
                    'alert_type': 'error',
                },
                'degraded': {
                    'icon': '🟡',
                    'title': texts.t('ADMIN_NOTIFY_PANEL_TITLE_DEGRADED', 'ПАНЕЛЬ REMNAWAVE РАБОТАЕТ СО СБОЯМИ'),
                    'alert_type': 'warning',
                },
                'maintenance': {
                    'icon': '🔧',
                    'title': texts.t('ADMIN_NOTIFY_PANEL_TITLE_MAINTENANCE', 'ПАНЕЛЬ REMNAWAVE НА ОБСЛУЖИВАНИИ'),
                    'alert_type': 'info',
                },
            }

            config = status_config.get(status, status_config['offline'])

            message_parts = [f'{config["icon"]} <b>{config["title"]}</b>', '']

            if details.get('api_url'):
                message_parts.append(
                    texts.t('ADMIN_NOTIFY_URL_LINE', '🔗 <b>URL:</b> {url}').format(url=details['api_url'])
                )

            if details.get('response_time'):
                message_parts.append(
                    texts.t('ADMIN_NOTIFY_RESPONSE_TIME_LINE', '⚡ <b>Время отклика:</b> {value} сек').format(
                        value=details['response_time']
                    )
                )

            if details.get('last_check'):
                last_check = details['last_check']
                if isinstance(last_check, str):
                    last_check = datetime.fromisoformat(last_check)
                message_parts.append(
                    texts.t('ADMIN_NOTIFY_LAST_CHECK_LINE', '🕐 <b>Последняя проверка:</b> {time}').format(
                        time=format_local_datetime(last_check, '%H:%M:%S')
                    )
                )

            if status == 'online':
                if details.get('uptime'):
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_UPTIME_LINE', '⏱️ <b>Время работы:</b> {uptime}').format(
                            uptime=details['uptime']
                        )
                    )

                if details.get('users_online'):
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_USERS_ONLINE_LINE', '👥 <b>Пользователей онлайн:</b> {count}').format(
                            count=details['users_online']
                        )
                    )

                message_parts.append('')
                message_parts.append(texts.t('ADMIN_NOTIFY_PANEL_ALL_OK', '✅ Все системы работают нормально.'))

            elif status == 'offline':
                if details.get('error'):
                    error_msg = str(details['error'])[:150]
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_ERROR_LINE', '❌ <b>Ошибка:</b> {error}').format(error=error_msg)
                    )

                if details.get('consecutive_failures'):
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_FAILURES_LINE', '🔄 <b>Неудачных попыток:</b> {count}').format(
                            count=details['consecutive_failures']
                        )
                    )

                message_parts.append('')
                message_parts.append(
                    texts.t(
                        'ADMIN_NOTIFY_PANEL_OFFLINE_HINT',
                        '⚠️ Панель недоступна. Проверьте соединение и статус сервера.',
                    )
                )

            elif status == 'degraded':
                if details.get('issues'):
                    issues = details['issues']
                    if isinstance(issues, list):
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_PANEL_ISSUES_HEADER', '⚠️ <b>Обнаруженные проблемы:</b>')
                        )
                        issue_template = texts.t('ADMIN_NOTIFY_PANEL_ISSUE_ITEM', '   • {issue}')
                        for issue in issues[:3]:
                            message_parts.append(issue_template.format(issue=issue))
                    else:
                        message_parts.append(
                            texts.t('ADMIN_NOTIFY_PANEL_ISSUE_LINE', '⚠️ <b>Проблема:</b> {issue}').format(
                                issue=issues
                            )
                        )

                message_parts.append('')
                message_parts.append(
                    texts.t('ADMIN_NOTIFY_PANEL_DEGRADED_HINT', 'Панель работает, но возможны задержки или сбои.')
                )

            elif status == 'maintenance':
                if details.get('maintenance_reason'):
                    message_parts.append(
                        texts.t('ADMIN_NOTIFY_MAINTENANCE_REASON_LINE', '🔧 <b>Причина:</b> {reason}').format(
                            reason=html.escape(details['maintenance_reason'])
                        )
                    )

                if details.get('estimated_duration'):
                    message_parts.append(
                        texts.t(
                            'ADMIN_NOTIFY_ESTIMATED_DURATION_LINE', '⏰ <b>Ожидаемая длительность:</b> {duration}'
                        ).format(duration=details['estimated_duration'])
                    )

                message_parts.append('')
                message_parts.append(
                    texts.t('ADMIN_NOTIFY_PANEL_MAINTENANCE_HINT', 'Панель временно недоступна для обслуживания.')
                )

            message_parts.append('')
            message_parts.append(
                texts.t('ADMIN_NOTIFY_TIMESTAMP_LINE', '⏰ <i>{timestamp}</i>').format(
                    timestamp=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
                )
            )

            message = '\n'.join(message_parts)

            return await self._send_message(message, category=NotificationCategory.INFRASTRUCTURE)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о статусе панели Remnawave', error=e)
            return False

    async def send_subscription_update_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        update_type: str,
        old_value: Any,
        new_value: Any,
        price_paid: int = 0,
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            texts = get_texts()
            # Определяем заголовок по типу операции
            update_titles = {
                'traffic': texts.t('ADMIN_NOTIFY_UPDATE_TITLE_TRAFFIC', '📊 ДОКУПКА ТРАФИКА'),
                'devices': texts.t('ADMIN_NOTIFY_UPDATE_TITLE_DEVICES', '📱 ДОКУПКА УСТРОЙСТВ'),
                'servers': texts.t('ADMIN_NOTIFY_UPDATE_TITLE_SERVERS', '🌐 СМЕНА СЕРВЕРОВ'),
            }
            title = update_titles.get(
                update_type, texts.t('ADMIN_NOTIFY_UPDATE_TITLE_DEFAULT', '⚙️ ИЗМЕНЕНИЕ ПОДПИСКИ')
            )

            # Получаем название тарифа
            tariff_name = await self._get_tariff_name(db, subscription)

            # Формируем компактное сообщение
            message_lines = [
                f'<b>{title}</b>',
                '',
                texts.t('ADMIN_NOTIFY_USER_COMPACT_LINE', '👤 {user} ({identifier})').format(
                    user=user_display, identifier=user_id_display
                ),
            ]

            # Добавляем username только если есть
            username = getattr(user, 'username', None)
            if username:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_USERNAME_COMPACT_LINE', '📱 {username}').format(
                        username=format_username_link(username)
                    )
                )

            # Тариф (если есть)
            if tariff_name:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_TARIFF_COMPACT_LINE', '🏷️ Тариф: <b>{name}</b>').format(name=tariff_name)
                )

            message_lines.append('')

            # Форматируем изменение в зависимости от типа
            change_template = texts.t('ADMIN_NOTIFY_UPDATE_CHANGE_LINE', '🔄 {old} → {new}')
            if update_type == 'servers':
                old_servers_info = await self._format_servers_detailed(old_value)
                new_servers_info = await self._format_servers_detailed(new_value)
                message_lines.append(change_template.format(old=old_servers_info, new=new_servers_info))
            elif update_type == 'traffic':
                old_formatted = self._format_update_value(old_value, update_type)
                new_formatted = self._format_update_value(new_value, update_type)
                message_lines.append(change_template.format(old=old_formatted, new=new_formatted))
            elif update_type == 'devices':
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_UPDATE_CHANGE_DEVICES', '🔄 {old} → {new} устр.').format(
                        old=old_value, new=new_value
                    )
                )
            else:
                message_lines.append(change_template.format(old=old_value, new=new_value))

            # Стоимость операции
            if price_paid > 0:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_UPDATE_PRICE_LINE', '💵 <b>{amount}</b>').format(
                        amount=settings.format_price(price_paid)
                    )
                )
            else:
                message_lines.append(texts.t('ADMIN_NOTIFY_UPDATE_FREE', '💸 Бесплатно'))

            message_lines.extend(
                [
                    '',
                    texts.t('ADMIN_NOTIFY_UPDATE_UNTIL_LINE', '📅 До {date}').format(
                        date=format_local_datetime(subscription.end_date, '%d.%m.%Y')
                    ),
                    texts.t('ADMIN_NOTIFY_BALANCE_LINE', '💰 Баланс: {amount}').format(
                        amount=settings.format_price(user.balance_kopeks)
                    ),
                ]
            )

            # Реферер (только если есть)
            if user.referred_by_id:
                referrer_info = await self._get_referrer_info(db, user.referred_by_id)
                if referrer_info != 'Нет':
                    message_lines.append(
                        texts.t('ADMIN_NOTIFY_REFERRER_COMPACT_LINE', '🔗 Реф: {referrer}').format(
                            referrer=referrer_info
                        )
                    )

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.ADDONS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об изменении подписки', error=e)
            return False

    async def _format_servers_detailed(self, server_uuids: list[str]) -> str:
        texts = get_texts()
        if not server_uuids:
            return texts.t('ADMIN_NOTIFY_SERVERS_DETAILED_NONE', 'Нет серверов')

        try:
            from app.handlers.subscription import get_servers_display_names

            servers_names = await get_servers_display_names(server_uuids)

            if servers_names and servers_names != 'Нет серверов':
                return texts.t('ADMIN_NOTIFY_SERVERS_DETAILED_COUNT_NAMES', '{count} серверов ({names})').format(
                    count=len(server_uuids), names=servers_names
                )
            return texts.t('ADMIN_NOTIFY_SERVERS_DETAILED_COUNT', '{count} серверов').format(count=len(server_uuids))

        except Exception as e:
            logger.warning('Ошибка получения названий серверов для уведомления', error=e)
            return texts.t('ADMIN_NOTIFY_SERVERS_DETAILED_COUNT', '{count} серверов').format(count=len(server_uuids))

    def _format_update_value(self, value: Any, update_type: str) -> str:
        texts = get_texts()
        if update_type == 'traffic':
            if value == 0:
                return texts.t('ADMIN_NOTIFY_UPDATE_VALUE_UNLIMITED', '♾ Безлимитный')
            return texts.t('ADMIN_NOTIFY_TRAFFIC_GB', '{traffic} ГБ').format(traffic=value)
        if update_type == 'devices':
            return texts.t('ADMIN_NOTIFY_UPDATE_VALUE_DEVICES', '{value} устройств').format(value=value)
        if update_type == 'servers':
            if isinstance(value, list):
                return texts.t('ADMIN_NOTIFY_SERVERS_DETAILED_COUNT', '{count} серверов').format(count=len(value))
            return str(value)
        return str(value)

    async def send_partner_application_notification(
        self,
        user: User,
        application_data: dict[str, Any],
    ) -> bool:
        """Уведомление о новой заявке на партнёрку."""
        if not self._is_enabled():
            return False

        try:
            texts = get_texts()
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                texts.t('ADMIN_NOTIFY_PARTNER_TITLE', '🤝 <b>ЗАЯВКА НА ПАРТНЁРКУ</b>'),
                '',
                texts.t('ADMIN_NOTIFY_USER_COMPACT_LINE', '👤 {user} ({identifier})').format(
                    user=user_display, identifier=user_id_display
                ),
            ]

            username = getattr(user, 'username', None)
            if username:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_USERNAME_COMPACT_LINE', '📱 {username}').format(
                        username=format_username_link(username)
                    )
                )

            message_lines.append('')

            if application_data.get('company_name'):
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_PARTNER_COMPANY', '🏢 Компания: {company}').format(
                        company=html.escape(str(application_data['company_name']))
                    )
                )
            if application_data.get('telegram_channel'):
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_PARTNER_CHANNEL', '📢 Канал: {channel}').format(
                        channel=html.escape(str(application_data['telegram_channel']))
                    )
                )
            if application_data.get('website_url'):
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_PARTNER_WEBSITE', '🌐 Сайт: {website}').format(
                        website=html.escape(str(application_data['website_url']))
                    )
                )
            if application_data.get('description'):
                desc = str(application_data['description'])
                if len(desc) > 200:
                    desc = desc[:197] + '...'
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_PARTNER_DESCRIPTION', '📝 {description}').format(
                        description=html.escape(desc)
                    )
                )
            if application_data.get('expected_monthly_referrals'):
                message_lines.append(
                    texts.t(
                        'ADMIN_NOTIFY_PARTNER_EXPECTED_REFERRALS', '👥 Ожидаемых рефералов: {count}/мес'
                    ).format(count=application_data['expected_monthly_referrals'])
                )
            if application_data.get('desired_commission_percent'):
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_PARTNER_COMMISSION', '💰 Желаемая комиссия: {percent}%').format(
                        percent=application_data['desired_commission_percent']
                    )
                )

            message_lines.extend(
                [
                    '',
                    texts.t('ADMIN_NOTIFY_TIMESTAMP_LINE', '⏰ <i>{timestamp}</i>').format(
                        timestamp=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
                    ),
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PARTNERS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о заявке на партнёрку', error=e)
            return False

    async def send_withdrawal_request_notification(
        self,
        user: User,
        amount_kopeks: int,
        payment_details: str | None = None,
    ) -> bool:
        """Уведомление о запросе на вывод средств."""
        if not self._is_enabled():
            return False

        try:
            texts = get_texts()
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                texts.t('ADMIN_NOTIFY_WITHDRAWAL_TITLE', '💸 <b>ЗАПРОС НА ВЫВОД СРЕДСТВ</b>'),
                '',
                texts.t('ADMIN_NOTIFY_USER_COMPACT_LINE', '👤 {user} ({identifier})').format(
                    user=user_display, identifier=user_id_display
                ),
            ]

            username = getattr(user, 'username', None)
            if username:
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_USERNAME_COMPACT_LINE', '📱 {username}').format(
                        username=format_username_link(username)
                    )
                )

            message_lines.extend(
                [
                    '',
                    texts.t('ADMIN_NOTIFY_WITHDRAWAL_AMOUNT', '💵 <b>Сумма: {amount}</b>').format(
                        amount=settings.format_price(amount_kopeks)
                    ),
                    texts.t('ADMIN_NOTIFY_BALANCE_LINE', '💰 Баланс: {amount}').format(
                        amount=settings.format_price(user.balance_kopeks)
                    ),
                ]
            )

            if payment_details:
                details = str(payment_details)
                if len(details) > 200:
                    details = details[:197] + '...'
                message_lines.extend(
                    [
                        '',
                        texts.t('ADMIN_NOTIFY_WITHDRAWAL_DETAILS', '💳 Реквизиты: {details}').format(
                            details=html.escape(details)
                        ),
                    ]
                )

            message_lines.extend(
                [
                    '',
                    texts.t('ADMIN_NOTIFY_TIMESTAMP_LINE', '⏰ <i>{timestamp}</i>').format(
                        timestamp=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
                    ),
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PARTNERS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о запросе на вывод', error=e)
            return False

    async def send_bulk_ban_notification(
        self,
        admin_user_id: int,
        successfully_banned: int,
        not_found: int,
        errors: int,
        admin_name: str = 'Администратор',
    ) -> bool:
        """Отправляет уведомление о массовой блокировке пользователей"""
        if not self._is_enabled():
            return False

        try:
            texts = get_texts()
            message_lines = [
                texts.t('ADMIN_NOTIFY_BULK_BAN_TITLE', '🛑 <b>МАССОВАЯ БЛОКИРОВКА ПОЛЬЗОВАТЕЛЕЙ</b>'),
                '',
                texts.t('ADMIN_NOTIFY_BULK_BAN_ADMIN', '👮 <b>Администратор:</b> {name}').format(
                    name=html.escape(admin_name)
                ),
                texts.t('ADMIN_NOTIFY_BULK_BAN_ADMIN_ID', '🆔 <b>ID администратора:</b> {id}').format(
                    id=admin_user_id
                ),
                '',
                texts.t('ADMIN_NOTIFY_BULK_BAN_RESULTS_HEADER', '📊 <b>Результаты:</b>'),
                texts.t('ADMIN_NOTIFY_BULK_BAN_SUCCESS', '✅ Успешно заблокировано: {count}').format(
                    count=successfully_banned
                ),
                texts.t('ADMIN_NOTIFY_BULK_BAN_NOT_FOUND', '❌ Не найдено: {count}').format(count=not_found),
                texts.t('ADMIN_NOTIFY_BULK_BAN_ERRORS', '💥 Ошибок: {count}').format(count=errors),
            ]

            total_processed = successfully_banned + not_found + errors
            if total_processed > 0:
                success_rate = (successfully_banned / total_processed) * 100
                message_lines.append(
                    texts.t('ADMIN_NOTIFY_BULK_BAN_SUCCESS_RATE', '📈 Успешность: {rate:.1f}%').format(
                        rate=success_rate
                    )
                )

            message_lines.extend(
                [
                    '',
                    texts.t('ADMIN_NOTIFY_TIMESTAMP_LINE', '⏰ <i>{timestamp}</i>').format(
                        timestamp=format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
                    ),
                ]
            )

            message = '\n'.join(message_lines)
            return await self._send_message(message, category=NotificationCategory.PARTNERS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о массовой блокировке', error=e)
            return False

    async def send_ticket_event_notification(
        self,
        text: str,
        keyboard: types.InlineKeyboardMarkup | None = None,
        *,
        media_file_id: str | None = None,
        media_type: str | None = None,
    ) -> bool:
        """Публичный метод для отправки уведомлений по тикетам в админ-топик.
        Учитывает настройки включенности в settings.
        Если передан media_file_id, отправляет медиа в тот же топик вместе с текстом.
        """
        # Respect runtime toggle for admin ticket notifications
        try:
            from app.services.support_settings_service import SupportSettingsService

            runtime_enabled = SupportSettingsService.get_admin_ticket_notifications_enabled()
        except Exception:
            runtime_enabled = True
        if not (self._is_enabled() and runtime_enabled):
            logger.info(
                'Ticket notification skipped',
                _is_enabled=self._is_enabled(),
                runtime_enabled=runtime_enabled,
            )
            return False

        # Если есть медиа, отправляем фото с текстом как caption (если влезает) или текст + фото
        if media_file_id and media_type == 'photo':
            return await self._send_ticket_photo_notification(text, media_file_id, keyboard)

        return await self._send_message(text, reply_markup=keyboard, category=NotificationCategory.TICKETS)

    async def _send_ticket_photo_notification(
        self,
        text: str,
        photo_file_id: str,
        keyboard: types.InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Отправить фото с текстом в тикет-топик.
        Если текст помещается в caption (≤1024 символов после парсинга HTML) — фото с caption.
        Иначе — сначала текст, потом фото в тот же топик.
        """
        if not self.chat_id:
            return False

        thread_id = self._resolve_topic_id(category=NotificationCategory.TICKETS)

        try:
            if not caption_exceeds_telegram_limit(text):
                # Фото с caption — всё в одном сообщении
                photo_kwargs: dict = {
                    'chat_id': self.chat_id,
                    'photo': photo_file_id,
                    'caption': text,
                    'parse_mode': 'HTML',
                }
                if thread_id:
                    photo_kwargs['message_thread_id'] = thread_id
                if keyboard:
                    photo_kwargs['reply_markup'] = keyboard
                await self.bot.send_photo(**photo_kwargs)
            else:
                # Текст отдельно, фото следом в тот же топик
                await self._send_message(text, reply_markup=keyboard, category=NotificationCategory.TICKETS)
                photo_kwargs = {
                    'chat_id': self.chat_id,
                    'photo': photo_file_id,
                }
                if thread_id:
                    photo_kwargs['message_thread_id'] = thread_id
                await self.bot.send_photo(**photo_kwargs)

            return True
        except Exception as e:
            logger.error('Ошибка отправки фото-уведомления тикета', error=e)
            # Fallback: отправляем хотя бы текст
            return await self._send_message(text, reply_markup=keyboard, category=NotificationCategory.TICKETS)

    async def send_suspicious_traffic_notification(self, message: str, bot: Bot, topic_id: int | None = None) -> bool:
        """
        Отправляет уведомление о подозрительной активности трафика

        Args:
            message: текст уведомления
            bot: экземпляр бота для отправки сообщения
            topic_id: ID топика для отправки уведомления (если не указан, использует стандартный)
        """
        if not self._is_enabled() or not self.category_enabled.get(NotificationCategory.INFRASTRUCTURE, True):
            return False

        # Используем специальный топик для подозрительной активности, если он задан
        notification_topic_id = topic_id or self.topic_id

        try:
            message_kwargs = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }

            if notification_topic_id:
                message_kwargs['message_thread_id'] = notification_topic_id

            await bot.send_message(**message_kwargs)
            logger.info(
                'Уведомление о подозрительной активности отправлено в чат топик',
                chat_id=self.chat_id,
                notification_topic_id=notification_topic_id,
            )
            return True

        except TelegramForbiddenError:
            logger.error('Бот не имеет прав для отправки в чат', chat_id=self.chat_id)
            return False
        except TelegramBadRequest as e:
            logger.error('Ошибка отправки уведомления о подозрительной активности', error=e)
            return False
        except Exception as e:
            logger.error('Неожиданная ошибка при отправке уведомления о подозрительной активности', error=e)
            return False
