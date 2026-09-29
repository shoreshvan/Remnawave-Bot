"""
Сервис для отправки уведомлений от ban системы пользователям
"""

import html

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.services.notification_delivery_service import (
    NotificationType,
    notification_delivery_service,
)
from app.services.remnawave_service import remnawave_service


logger = structlog.get_logger(__name__)


def _format_notification_template(template: str, fallback: str, **values: object) -> str:
    """Format an editable template and fall back safely when its placeholders are invalid."""
    try:
        return template.format(**values)
    except (IndexError, KeyError, ValueError):
        logger.exception('Некорректный шаблон ban-уведомления, использован резервный')
        return fallback.format(**values)


def get_delete_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой удаления уведомления"""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=get_texts(settings.DEFAULT_LANGUAGE).t(
            'BAN_NOTIFY_DELETE_BUTTON',
            '🗑 Удалить',
        ), callback_data='ban_notify:delete')]]
    )


class BanNotificationService:
    """Сервис для отправки уведомлений о банах пользователям"""

    def __init__(self):
        self._bot: Bot | None = None

    def set_bot(self, bot: Bot):
        """Установить инстанс бота для отправки сообщений"""
        self._bot = bot

    async def _find_user_by_identifier(self, db: AsyncSession, user_identifier: str) -> User | None:
        """
        Найти пользователя по email или user_id из Remnawave Panel

        Args:
            db: Сессия БД
            user_identifier: Email или user_id пользователя

        Returns:
            User или None если не найден
        """
        # Сначала пытаемся получить telegram_id через remnawave_service
        try:
            telegram_id = await remnawave_service.get_telegram_id_by_email(user_identifier)
            if telegram_id:
                # Ищем пользователя по telegram_id
                result = await db.execute(select(User).where(User.telegram_id == telegram_id))
                user = result.scalar_one_or_none()
                if user:
                    return user
        except Exception as e:
            logger.warning('Не удалось получить telegram_id через remnawave', error=e)

        # Если не нашли через remnawave, пытаемся искать по email в подписках
        # (это может быть полезно если у пользователя есть подписка с таким email)
        try:
            # Импортируем здесь чтобы избежать циклических импортов
            from app.database.models import Subscription

            result = await db.execute(
                select(User).join(Subscription).where(Subscription.email == user_identifier).limit(1)
            )
            user = result.scalar_one_or_none()
            if user:
                return user
        except Exception as e:
            logger.warning('Ошибка поиска пользователя по email в подписках', error=e)

        return None

    async def send_punishment_notification(
        self,
        db: AsyncSession,
        user_identifier: str,
        username: str,
        ip_count: int,
        limit: int,
        ban_minutes: int,
        node_name: str | None = None,
        revoke: bool = False,
    ) -> tuple[bool, str, int | None]:
        """
        Отправить уведомление о блокировке пользователю

        Returns:
            (success, message, telegram_id)
        """
        texts = get_texts(settings.DEFAULT_LANGUAGE)
        if not self._bot:
            return False, texts.t(
                'BAN_NOTIFY_BOT_UNAVAILABLE',
                'Бот не инициализирован',
            ), None
        if not settings.is_notifications_enabled():
            return False, texts.t(
                'BAN_NOTIFY_DISABLED',
                'Уведомления пользователям отключены',
            ), None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return False, texts.t(
                'BAN_NOTIFY_USER_NOT_FOUND',
                'Пользователь не найден: {user_identifier}',
            ).format(
                user_identifier=user_identifier,
            ), None
        texts = get_texts(getattr(user, 'language', settings.DEFAULT_LANGUAGE))

        # Формируем информацию о ноде (заметно выделяем)
        node_info = texts.t(
            'BAN_NOTIFY_NODE_INFO',
            '🖥 <b>Нода:</b> <code>{node_name}</code>',
        ).format(
            node_name=html.escape(node_name),
        ) if node_name else ''

        # Формируем сообщение из настроек
        # Используем безопасное форматирование - если {node_info} отсутствует в шаблоне, не будет ошибки
        format_vars = {'ip_count': ip_count, 'limit': limit, 'ban_minutes': ban_minutes, 'node_info': node_info}
        template = settings.BAN_MSG_REVOKE if revoke else settings.BAN_MSG_PUNISHMENT
        message_text = _format_notification_template(
            template,
            texts.t(
                'BAN_NOTIFY_PUNISHMENT_FALLBACK',
                '🚫 <b>АККАУНТ ЗАБЛОКИРОВАН</b>\n'
                '\n'
                '{node_info}\n'
                '📱 Устройств: <b>{ip_count}</b> из <b>{limit}</b>\n'
                '⏱ Ограничение: <b>{ban_minutes} мин</b>',
            ),
            **format_vars,
        )

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            action = texts.t(
                'BAN_NOTIFY_REVOKED_ACTION',
                'Ключи доступа сброшены',
            ) if revoke else texts.t(
                'BAN_NOTIFY_BAN_ACTION',
                'Бан на {ban_minutes} минут',
            ).format(
                ban_minutes=ban_minutes,
            )
            reason = texts.t(
                'BAN_NOTIFY_IP_REASON',
                'IP лимит превышен: {ip_count}/{limit}. {action}.',
            ).format(
                ip_count=ip_count,
                limit=limit,
                action=action,
            )
            if node_name:
                reason += texts.t(
                    'BAN_NOTIFY_NODE_SUFFIX',
                    ' Нода: {node_name}',
                ).format(
                    node_name=node_name,
                )
            success = await notification_delivery_service.notify_ban(
                user=user,
                reason=reason,
            )
            if success:
                logger.info('Email уведомление о бане отправлено пользователю', user_id=user.id)
                return True, texts.t(
                    'BAN_NOTIFY_EMAIL_SENT',
                    'Email уведомление отправлено',
                ), None
            return False, texts.t(
                'BAN_NOTIFY_EMAIL_FAILED',
                'Не удалось отправить email уведомление',
            ), None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id, text=message_text, parse_mode='HTML', reply_markup=get_delete_keyboard()
            )
            logger.info(
                'Уведомление о бане отправлено пользователю',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, texts.t(
                'BAN_NOTIFY_SENT',
                'Уведомление отправлено',
            ), user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки уведомления пользователю',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return False, texts.t(
                'BAN_NOTIFY_TELEGRAM_ERROR',
                'Ошибка Telegram API: {error!s}',
            ).format(
                error=e,
            ), user.telegram_id

    async def send_enabled_notification(
        self, db: AsyncSession, user_identifier: str, username: str
    ) -> tuple[bool, str, int | None]:
        """
        Отправить уведомление о разблокировке пользователю

        Returns:
            (success, message, telegram_id)
        """
        texts = get_texts(settings.DEFAULT_LANGUAGE)
        if not self._bot:
            return False, texts.t(
                'BAN_NOTIFY_BOT_UNAVAILABLE',
                'Бот не инициализирован',
            ), None
        if not settings.is_notifications_enabled():
            return False, texts.t(
                'BAN_NOTIFY_DISABLED',
                'Уведомления пользователям отключены',
            ), None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return False, texts.t(
                'BAN_NOTIFY_USER_NOT_FOUND',
                'Пользователь не найден: {user_identifier}',
            ).format(
                user_identifier=user_identifier,
            ), None
        texts = get_texts(getattr(user, 'language', settings.DEFAULT_LANGUAGE))

        # Формируем сообщение из настроек
        message_text = settings.BAN_MSG_ENABLED

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            success = await notification_delivery_service.notify_unban(user=user)
            if success:
                logger.info('Email уведомление о разбане отправлено пользователю', user_id=user.id)
                return True, texts.t(
                    'BAN_NOTIFY_EMAIL_SENT',
                    'Email уведомление отправлено',
                ), None
            return False, texts.t(
                'BAN_NOTIFY_EMAIL_FAILED',
                'Не удалось отправить email уведомление',
            ), None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id, text=message_text, parse_mode='HTML', reply_markup=get_delete_keyboard()
            )
            logger.info(
                'Уведомление о разбане отправлено пользователю',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, texts.t(
                'BAN_NOTIFY_SENT',
                'Уведомление отправлено',
            ), user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки уведомления пользователю',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return False, texts.t(
                'BAN_NOTIFY_TELEGRAM_ERROR',
                'Ошибка Telegram API: {error!s}',
            ).format(
                error=e,
            ), user.telegram_id

    async def send_warning_notification(
        self, db: AsyncSession, user_identifier: str, username: str, warning_message: str
    ) -> tuple[bool, str, int | None]:
        """
        Отправить предупреждение пользователю

        Returns:
            (success, message, telegram_id)
        """
        texts = get_texts(settings.DEFAULT_LANGUAGE)
        if not self._bot:
            return False, texts.t(
                'BAN_NOTIFY_BOT_UNAVAILABLE',
                'Бот не инициализирован',
            ), None
        if not settings.is_notifications_enabled():
            return False, texts.t(
                'BAN_NOTIFY_DISABLED',
                'Уведомления пользователям отключены',
            ), None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return False, texts.t(
                'BAN_NOTIFY_USER_NOT_FOUND',
                'Пользователь не найден: {user_identifier}',
            ).format(
                user_identifier=user_identifier,
            ), None
        texts = get_texts(getattr(user, 'language', settings.DEFAULT_LANGUAGE))

        # Формируем сообщение из настроек
        safe_warning = html.escape(warning_message)
        message_text = _format_notification_template(
            settings.BAN_MSG_WARNING,
            texts.t(
                'BAN_NOTIFY_WARNING_FALLBACK',
                '⚠️ <b>ПРЕДУПРЕЖДЕНИЕ</b>\n'
                '\n'
                '{warning_message}',
            ),
            warning_message=safe_warning,
        )

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            context = {'message': warning_message}
            success = await notification_delivery_service.send_notification(
                user=user,
                notification_type=NotificationType.WARNING_NOTIFICATION,
                context=context,
            )
            if success:
                logger.info('Email предупреждение отправлено пользователю', user_id=user.id)
                return True, texts.t(
                    'BAN_NOTIFY_EMAIL_WARNING_SENT',
                    'Email предупреждение отправлено',
                ), None
            return False, texts.t(
                'BAN_NOTIFY_EMAIL_WARNING_FAILED',
                'Не удалось отправить email предупреждение',
            ), None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id, text=message_text, parse_mode='HTML', reply_markup=get_delete_keyboard()
            )
            logger.info(
                'Предупреждение отправлено пользователю',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, texts.t(
                'BAN_NOTIFY_WARNING_SENT',
                'Предупреждение отправлено',
            ), user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки предупреждения пользователю',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return False, texts.t(
                'BAN_NOTIFY_TELEGRAM_ERROR',
                'Ошибка Telegram API: {error!s}',
            ).format(
                error=e,
            ), user.telegram_id

    async def send_network_wifi_notification(
        self,
        db: AsyncSession,
        user_identifier: str,
        username: str,
        ban_minutes: int,
        network_type: str | None = None,
        node_name: str | None = None,
    ) -> tuple[bool, str, int | None]:
        """
        Отправить уведомление о блокировке за использование WiFi сети

        Returns:
            (success, message, telegram_id)
        """
        texts = get_texts(settings.DEFAULT_LANGUAGE)
        if not self._bot:
            return False, texts.t(
                'BAN_NOTIFY_BOT_UNAVAILABLE',
                'Бот не инициализирован',
            ), None
        if not settings.is_notifications_enabled():
            return False, texts.t(
                'BAN_NOTIFY_DISABLED',
                'Уведомления пользователям отключены',
            ), None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return False, texts.t(
                'BAN_NOTIFY_USER_NOT_FOUND',
                'Пользователь не найден: {user_identifier}',
            ).format(
                user_identifier=user_identifier,
            ), None
        texts = get_texts(getattr(user, 'language', settings.DEFAULT_LANGUAGE))

        # Формируем сообщение из настроек (заметно выделяем)
        network_info = texts.t(
            'BAN_NOTIFY_NETWORK_INFO',
            '├ 🌐 Сеть: <b>{network_type}</b>\n',
        ).format(
            network_type=html.escape(network_type),
        ) if network_type else ''
        node_info = texts.t(
            'BAN_NOTIFY_NODE_INFO',
            '🖥 <b>Нода:</b> <code>{node_name}</code>',
        ).format(
            node_name=html.escape(node_name),
        ) if node_name else ''

        logger.info('WiFi notification', node_name=repr(node_name), node_info=repr(node_info))

        # Безопасное форматирование
        format_vars = {'ban_minutes': ban_minutes, 'network_info': network_info, 'node_info': node_info}
        message_text = _format_notification_template(
            settings.BAN_MSG_WIFI,
            texts.t(
                'BAN_NOTIFY_WIFI_FALLBACK',
                '🚫 <b>Блокировка за WiFi</b>\n'
                '\n'
                '{node_info}\n'
                '{network_info}⏱ Время блокировки: <b>{ban_minutes} мин</b>',
            ),
            **format_vars,
        )

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            reason = texts.t(
                'BAN_NOTIFY_WIFI_REASON',
                'Использование WiFi сети запрещено. Бан на {ban_minutes} минут.',
            ).format(
                ban_minutes=ban_minutes,
            )
            if network_type:
                reason += texts.t(
                    'BAN_NOTIFY_NETWORK_SUFFIX',
                    ' Сеть: {network_type}',
                ).format(
                    network_type=network_type,
                )
            if node_name:
                reason += texts.t(
                    'BAN_NOTIFY_NODE_SUFFIX',
                    ' Нода: {node_name}',
                ).format(
                    node_name=node_name,
                )
            success = await notification_delivery_service.notify_ban(
                user=user,
                reason=reason,
            )
            if success:
                logger.info('Email WiFi уведомление отправлено пользователю', user_id=user.id)
                return True, texts.t(
                    'BAN_NOTIFY_EMAIL_SENT',
                    'Email уведомление отправлено',
                ), None
            return False, texts.t(
                'BAN_NOTIFY_EMAIL_FAILED',
                'Не удалось отправить email уведомление',
            ), None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id, text=message_text, parse_mode='HTML', reply_markup=get_delete_keyboard()
            )
            logger.info(
                'Уведомление о WiFi бане отправлено пользователю',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, texts.t(
                'BAN_NOTIFY_SENT',
                'Уведомление отправлено',
            ), user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки WiFi уведомления пользователю',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return False, texts.t(
                'BAN_NOTIFY_TELEGRAM_ERROR',
                'Ошибка Telegram API: {error!s}',
            ).format(
                error=e,
            ), user.telegram_id

    async def send_network_mobile_notification(
        self,
        db: AsyncSession,
        user_identifier: str,
        username: str,
        ban_minutes: int,
        network_type: str | None = None,
        node_name: str | None = None,
    ) -> tuple[bool, str, int | None]:
        """
        Отправить уведомление о блокировке за использование мобильной сети

        Returns:
            (success, message, telegram_id)
        """
        texts = get_texts(settings.DEFAULT_LANGUAGE)
        if not self._bot:
            return False, texts.t(
                'BAN_NOTIFY_BOT_UNAVAILABLE',
                'Бот не инициализирован',
            ), None
        if not settings.is_notifications_enabled():
            return False, texts.t(
                'BAN_NOTIFY_DISABLED',
                'Уведомления пользователям отключены',
            ), None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return False, texts.t(
                'BAN_NOTIFY_USER_NOT_FOUND',
                'Пользователь не найден: {user_identifier}',
            ).format(
                user_identifier=user_identifier,
            ), None
        texts = get_texts(getattr(user, 'language', settings.DEFAULT_LANGUAGE))

        # Формируем сообщение из настроек (заметно выделяем)
        network_info = texts.t(
            'BAN_NOTIFY_NETWORK_INFO',
            '├ 🌐 Сеть: <b>{network_type}</b>\n',
        ).format(
            network_type=html.escape(network_type),
        ) if network_type else ''
        node_info = texts.t(
            'BAN_NOTIFY_NODE_INFO',
            '🖥 <b>Нода:</b> <code>{node_name}</code>',
        ).format(
            node_name=html.escape(node_name),
        ) if node_name else ''

        # Безопасное форматирование
        format_vars = {'ban_minutes': ban_minutes, 'network_info': network_info, 'node_info': node_info}
        message_text = _format_notification_template(
            settings.BAN_MSG_MOBILE,
            texts.t(
                'BAN_NOTIFY_MOBILE_FALLBACK',
                '🚫 <b>Блокировка за мобильную сеть</b>\n'
                '\n'
                '{node_info}\n'
                '{network_info}⏱ Время блокировки: <b>{ban_minutes} мин</b>',
            ),
            **format_vars,
        )

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            reason = texts.t(
                'BAN_NOTIFY_MOBILE_REASON',
                'Использование мобильной сети запрещено. Бан на {ban_minutes} минут.',
            ).format(
                ban_minutes=ban_minutes,
            )
            if network_type:
                reason += texts.t(
                    'BAN_NOTIFY_NETWORK_SUFFIX',
                    ' Сеть: {network_type}',
                ).format(
                    network_type=network_type,
                )
            if node_name:
                reason += texts.t(
                    'BAN_NOTIFY_NODE_SUFFIX',
                    ' Нода: {node_name}',
                ).format(
                    node_name=node_name,
                )
            success = await notification_delivery_service.notify_ban(
                user=user,
                reason=reason,
            )
            if success:
                logger.info('Email Mobile уведомление отправлено пользователю', user_id=user.id)
                return True, texts.t(
                    'BAN_NOTIFY_EMAIL_SENT',
                    'Email уведомление отправлено',
                ), None
            return False, texts.t(
                'BAN_NOTIFY_EMAIL_FAILED',
                'Не удалось отправить email уведомление',
            ), None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id, text=message_text, parse_mode='HTML', reply_markup=get_delete_keyboard()
            )
            logger.info(
                'Уведомление о Mobile бане отправлено пользователю',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, texts.t(
                'BAN_NOTIFY_SENT',
                'Уведомление отправлено',
            ), user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки Mobile уведомления пользователю',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return False, texts.t(
                'BAN_NOTIFY_TELEGRAM_ERROR',
                'Ошибка Telegram API: {error!s}',
            ).format(
                error=e,
            ), user.telegram_id

    async def send_typed_ban_notification(
        self,
        db: AsyncSession,
        user_identifier: str,
        username: str,
        notification_type: str,
        ban_minutes: int,
        reason: str | None = None,
        node_name: str | None = None,
    ) -> tuple[bool, str, int | None]:
        """Send a BanHammer ban notification using a template for its cause."""
        texts = get_texts(settings.DEFAULT_LANGUAGE)
        if not self._bot:
            return False, texts.t(
                'BAN_NOTIFY_BOT_UNAVAILABLE',
                'Бот не инициализирован',
            ), None
        # Тот же глобальный рубильник, что и у остальных методов сервиса:
        # без него выключенные уведомления всё равно доставляли BanHammer-сообщения
        # в Telegram, хотя email-ветка ниже уже уходит через
        # notification_delivery_service, который рубильник уважает.
        if not settings.is_notifications_enabled():
            return False, texts.t(
                'BAN_NOTIFY_DISABLED',
                'Уведомления пользователям отключены',
            ), None

        template_names = {
            'torrent': 'BAN_MSG_TORRENT',
            'hwid_limit': 'BAN_MSG_HWID_LIMIT',
            'suspicious_destination': 'BAN_MSG_SUSPICIOUS_DESTINATION',
            'traffic_limit': 'BAN_MSG_TRAFFIC_LIMIT',
            'manual': 'BAN_MSG_MANUAL',
        }
        template_name = template_names.get(notification_type)
        if not template_name:
            logger.warning('Неизвестный типизированный тип бана', notification_type=notification_type)
            return False, texts.t(
                'BAN_NOTIFY_UNKNOWN_TYPE',
                'Неизвестный тип бана: {notification_type}',
            ).format(
                notification_type=notification_type,
            ), None

        user = await self._find_user_by_identifier(db, user_identifier)
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return False, texts.t(
                'BAN_NOTIFY_USER_NOT_FOUND',
                'Пользователь не найден: {user_identifier}',
            ).format(
                user_identifier=user_identifier,
            ), None
        texts = get_texts(getattr(user, 'language', settings.DEFAULT_LANGUAGE))

        template = getattr(settings, template_name)
        safe_reason = html.escape(reason or texts.t(
            'BAN_NOTIFY_NO_DETAILS',
            'Детали нарушения не указаны',
        ))
        node_info = texts.t(
            'BAN_NOTIFY_NODE_INFO',
            '🖥 <b>Нода:</b> <code>{node_name}</code>',
        ).format(
            node_name=html.escape(node_name),
        ) if node_name else ''
        message_text = _format_notification_template(
            template,
            texts.t(
                'BAN_NOTIFY_TYPED_FALLBACK',
                '🚫 <b>АККАУНТ ЗАБЛОКИРОВАН</b>\n'
                '\n'
                '{node_info}\n'
                '📝 <b>Детали:</b> {reason}\n'
                '⏱ <b>Время блокировки:</b> {ban_minutes} мин\n'
                '\n'
                '🔄 Доступ восстановится автоматически после окончания блокировки.',
            ),
            ban_minutes=ban_minutes,
            reason=safe_reason,
            node_info=node_info,
        )

        if not user.telegram_id:
            email_reason = reason or texts.t(
                'BAN_NOTIFY_RULES_REASON',
                'Нарушение правил сервиса',
            )
            if node_name:
                email_reason += texts.t(
                    'BAN_NOTIFY_NODE_SUFFIX',
                    ' Нода: {node_name}',
                ).format(
                    node_name=node_name,
                )
            success = await notification_delivery_service.notify_ban(user=user, reason=email_reason)
            if success:
                return True, texts.t(
                    'BAN_NOTIFY_EMAIL_SENT',
                    'Email уведомление отправлено',
                ), None
            return False, texts.t(
                'BAN_NOTIFY_EMAIL_FAILED',
                'Не удалось отправить email уведомление',
            ), None

        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=get_delete_keyboard(),
            )
            logger.info(
                'Типизированное уведомление о бане отправлено пользователю',
                notification_type=notification_type,
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, texts.t(
                'BAN_NOTIFY_SENT',
                'Уведомление отправлено',
            ), user.telegram_id
        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки типизированного уведомления о бане',
                notification_type=notification_type,
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return False, texts.t(
                'BAN_NOTIFY_TELEGRAM_ERROR',
                'Ошибка Telegram API: {error!s}',
            ).format(
                error=e,
            ), user.telegram_id


# Глобальный экземпляр сервиса
ban_notification_service = BanNotificationService()
