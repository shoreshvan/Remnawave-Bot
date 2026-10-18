"""
Сервис для обработки запросов на вывод реферального баланса
с анализом на подозрительную активность (отмывание денег).
"""

import html
import json
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.localization.texts import get_texts
from app.database.models import (
    ReferralEarning,
    Transaction,
    TransactionType,
    User,
    WithdrawalRequest,
    WithdrawalRequestStatus,
)


logger = structlog.get_logger(__name__)


class ReferralWithdrawalService:
    """Сервис для обработки запросов на вывод реферального баланса."""

    # ==================== МЕТОДЫ РАСЧЁТА БАЛАНСОВ ====================

    async def get_total_referral_earnings(self, db: AsyncSession, user_id: int) -> int:
        """
        Получает ОБЩУЮ сумму реферальных начислений (за всё время).
        Возвращает сумму в копейках.
        """
        result = await db.execute(
            select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(ReferralEarning.user_id == user_id)
        )
        return result.scalar() or 0

    async def get_user_own_deposits(self, db: AsyncSession, user_id: int) -> int:
        """
        Получает сумму собственных пополнений пользователя (НЕ реферальные).
        Фильтрует по payment_method IS NOT NULL — реальные платежи всегда имеют payment_method.
        """
        result = await db.execute(
            select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
                Transaction.user_id == user_id,
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.is_completed == True,
                Transaction.payment_method.isnot(None),
            )
        )
        return result.scalar() or 0

    async def get_first_referral_earning_date(self, db: AsyncSession, user_id: int) -> datetime | None:
        """
        Получает дату первого реферального начисления.
        """
        result = await db.execute(
            select(func.min(ReferralEarning.created_at)).where(ReferralEarning.user_id == user_id)
        )
        return result.scalar()

    async def get_user_spending(self, db: AsyncSession, user_id: int) -> int:
        """
        Получает сумму трат пользователя (покупки подписок, сброс трафика и т.д.).
        """
        result = await db.execute(
            select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
                Transaction.user_id == user_id,
                Transaction.type.in_([TransactionType.SUBSCRIPTION_PAYMENT.value, TransactionType.WITHDRAWAL.value]),
                Transaction.is_completed == True,
            )
        )
        return abs(result.scalar() or 0)

    async def get_user_spending_after_first_earning(self, db: AsyncSession, user_id: int) -> int:
        """
        Получает сумму трат ПОСЛЕ первого реферального начисления.
        Только эти траты могут быть засчитаны как "потрачено из реф. баланса".
        """
        first_earning_date = await self.get_first_referral_earning_date(db, user_id)
        if not first_earning_date:
            return 0

        result = await db.execute(
            select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
                Transaction.user_id == user_id,
                Transaction.type.in_([TransactionType.SUBSCRIPTION_PAYMENT.value, TransactionType.WITHDRAWAL.value]),
                Transaction.is_completed == True,
                Transaction.created_at >= first_earning_date,
            )
        )
        return abs(result.scalar() or 0)

    async def get_withdrawn_amount(self, db: AsyncSession, user_id: int) -> int:
        """
        Получает сумму уже выведенных средств (одобренные/выполненные заявки).
        """
        result = await db.execute(
            select(func.coalesce(func.sum(WithdrawalRequest.amount_kopeks), 0)).where(
                WithdrawalRequest.user_id == user_id,
                WithdrawalRequest.status.in_(
                    [WithdrawalRequestStatus.APPROVED.value, WithdrawalRequestStatus.COMPLETED.value]
                ),
            )
        )
        return result.scalar() or 0

    async def get_pending_withdrawal_amount(self, db: AsyncSession, user_id: int) -> int:
        """
        Получает сумму заявок в ожидании (заморожено).
        """
        result = await db.execute(
            select(func.coalesce(func.sum(WithdrawalRequest.amount_kopeks), 0)).where(
                WithdrawalRequest.user_id == user_id, WithdrawalRequest.status == WithdrawalRequestStatus.PENDING.value
            )
        )
        return result.scalar() or 0

    async def get_referral_balance_stats(self, db: AsyncSession, user_id: int) -> dict:
        """
        Получает полную статистику реферального баланса.

        Доступный реферальный баланс = min(баланс кошелька, заработано - выведено - в ожидании).
        Партнёр не может вывести больше, чем реально лежит в кошельке (User.balance_kopeks),
        и не больше, чем заработал минус уже выведенные/замороженные средства.
        """
        total_earned = await self.get_total_referral_earnings(db, user_id)
        own_deposits = await self.get_user_own_deposits(db, user_id)
        spending = await self.get_user_spending(db, user_id)
        spending_after_earning = await self.get_user_spending_after_first_earning(db, user_id)
        withdrawn = await self.get_withdrawn_amount(db, user_id)
        pending = await self.get_pending_withdrawal_amount(db, user_id)

        # Текущий баланс кошелька — реальный ограничитель вывода
        user = await db.get(User, user_id)
        user_balance = user.balance_kopeks if user else 0

        # referral_spent — для аналитики/отображения, больше НЕ влияет на available_referral
        referral_spent = min(spending_after_earning, total_earned)

        # Реферальное право: сколько заработано минус выведено/заморожено
        referral_entitlement = max(0, total_earned - withdrawn - pending)

        # Доступный реферальный баланс: мин(кошелёк, реферальное право)
        # Нельзя вывести больше, чем лежит в кошельке
        available_referral = min(user_balance, referral_entitlement)

        # Если разрешено выводить и свой баланс
        if not settings.REFERRAL_WITHDRAWAL_ONLY_REFERRAL_BALANCE:
            # Весь кошелёк доступен к выводу (уже ограничен user_balance)
            available_total = user_balance
        else:
            available_total = available_referral

        return {
            'total_earned': total_earned,  # Всего заработано с рефералов
            'own_deposits': own_deposits,  # Собственные пополнения
            'spending': spending,  # Потрачено на подписки и пр.
            'referral_spent': referral_spent,  # Сколько реф. баланса потрачено (аналитика)
            'withdrawn': withdrawn,  # Уже выведено
            'pending': pending,  # На рассмотрении
            'available_referral': available_referral,  # Доступно реф. баланса
            'available_total': available_total,  # Всего доступно к выводу
            'only_referral_mode': settings.REFERRAL_WITHDRAWAL_ONLY_REFERRAL_BALANCE,
        }

    async def get_available_for_withdrawal(self, db: AsyncSession, user_id: int) -> int:
        """Получает сумму, доступную для вывода."""
        stats = await self.get_referral_balance_stats(db, user_id)
        return stats['available_total']

    # ==================== ПРОВЕРКИ ====================

    async def get_last_withdrawal_request(self, db: AsyncSession, user_id: int) -> WithdrawalRequest | None:
        """Получает последнюю заявку на вывод пользователя."""
        result = await db.execute(
            select(WithdrawalRequest)
            .where(WithdrawalRequest.user_id == user_id)
            .order_by(WithdrawalRequest.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def can_request_withdrawal(
        self, db: AsyncSession, user_id: int, *, stats: dict | None = None
    ) -> tuple[bool, str, dict]:
        """
        Проверяет, может ли пользователь запросить вывод.
        Возвращает (can_request, reason, stats).
        Принимает предвычисленные stats для избежания повторного запроса.
        """
        texts = get_texts()

        if not settings.is_referral_withdrawal_enabled():
            return (
                False,
                texts.t('REFERRAL_WITHDRAWAL_DISABLED', 'Функция вывода реферального баланса отключена'),
                {
                    'total_earned': 0,
                    'own_deposits': 0,
                    'spending': 0,
                    'referral_spent': 0,
                    'withdrawn': 0,
                    'pending': 0,
                    'available_referral': 0,
                    'available_total': 0,
                    'only_referral_mode': settings.REFERRAL_WITHDRAWAL_ONLY_REFERRAL_BALANCE,
                },
            )

        # Проверяем доступный баланс
        if stats is None:
            stats = await self.get_referral_balance_stats(db, user_id)
        available = stats['available_total']
        min_amount = settings.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS

        if available < min_amount:
            return False, texts.t('REFERRAL_WITHDRAWAL_MIN_AMOUNT_CHECK', 'Минимальная сумма вывода: {min_amount} تومان. Доступно: {available} تومان').format(min_amount=f'{min_amount / 100:.0f}', available=f'{available / 100:.0f}'), stats

        # Проверяем cooldown (пропускаем в тестовом режиме)
        last_request = await self.get_last_withdrawal_request(db, user_id)
        if last_request:
            # В тестовом режиме пропускаем проверку cooldown
            if not settings.REFERRAL_WITHDRAWAL_TEST_MODE:
                cooldown_days = settings.REFERRAL_WITHDRAWAL_COOLDOWN_DAYS
                cooldown_end = last_request.created_at + timedelta(days=cooldown_days)

                if datetime.now(UTC) < cooldown_end:
                    days_left = (cooldown_end - datetime.now(UTC)).days + 1
                    return False, texts.t('REFERRAL_WITHDRAWAL_COOLDOWN', 'Следующий запрос на вывод будет доступен через {days} дн.').format(days=days_left), stats

            # Проверяем, нет ли активной заявки
            if last_request.status == WithdrawalRequestStatus.PENDING.value:
                return False, texts.t('REFERRAL_WITHDRAWAL_ALREADY_PENDING', 'У вас уже есть активная заявка на рассмотрении'), stats

        return True, 'OK', stats

    # ==================== АНАЛИЗ НА ОТМЫВАНИЕ ====================

    async def analyze_for_money_laundering(self, db: AsyncSession, user_id: int) -> dict:
        """
        Детальный анализ активности пользователя на предмет отмывания денег.
        """
        analysis = {'risk_score': 0, 'risk_level': 'low', 'recommendation': 'approve', 'flags': [], 'details': {}}

        texts = get_texts()

        # Получаем статистику баланса
        balance_stats = await self.get_referral_balance_stats(db, user_id)
        analysis['details']['balance_stats'] = balance_stats

        # 1. ПРОВЕРКА: Пользователь пополнил но не покупал подписки
        own_deposits = balance_stats['own_deposits']
        spending = balance_stats['spending']
        ratio_threshold = settings.REFERRAL_WITHDRAWAL_SUSPICIOUS_NO_PURCHASES_RATIO

        if own_deposits > 0 and spending == 0:
            analysis['risk_score'] += 40
            analysis['flags'].append(
                texts.t('REFERRAL_WITHDRAWAL_FLAG_NO_PURCHASE', '🔴 Пополнил {amount} تومان, но ничего не покупал!').format(
                    amount=f'{own_deposits / 100:.0f}'
                )
            )
        elif own_deposits > spending * ratio_threshold and spending > 0:
            analysis['risk_score'] += 25
            analysis['flags'].append(
                texts.t('REFERRAL_WITHDRAWAL_FLAG_LOW_SPENDING', '🟠 Пополнил {deposits} تومان, потратил только {spent} تومان').format(
                    deposits=f'{own_deposits / 100:.0f}', spent=f'{spending / 100:.0f}'
                )
            )

        # 2. Получаем информацию о рефералах
        referrals = await db.execute(select(User).where(User.referred_by_id == user_id))
        referrals_list = referrals.scalars().all()
        referral_count = len(referrals_list)
        analysis['details']['referral_count'] = referral_count

        if referral_count == 0 and balance_stats['total_earned'] > 0:
            analysis['risk_score'] += 50
            analysis['flags'].append(texts.t('REFERRAL_WITHDRAWAL_FLAG_NO_REFERRALS', '🔴 Нет рефералов, но есть реферальный доход!'))

        # 3. Анализ пополнений каждого реферала
        referral_ids = [r.id for r in referrals_list]
        suspicious_referrals = []

        if referral_ids:
            month_ago = datetime.now(UTC) - timedelta(days=30)

            # Одним запросом получаем статистику реальных пополнений всех рефералов за месяц
            # (исключаем промо-бонусы с payment_method=NULL)
            ref_deposits_result = await db.execute(
                select(
                    Transaction.user_id,
                    func.count().label('count'),
                    func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('total'),
                )
                .where(
                    Transaction.user_id.in_(referral_ids),
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed == True,
                    Transaction.created_at >= month_ago,
                    Transaction.payment_method.isnot(None),
                )
                .group_by(Transaction.user_id)
            )
            ref_deposit_map = {row.user_id: (row.count, row.total) for row in ref_deposits_result.all()}

            referrals_by_id = {r.id: r for r in referrals_list}
            max_deposits = settings.REFERRAL_WITHDRAWAL_SUSPICIOUS_MAX_DEPOSITS_PER_MONTH
            min_suspicious = settings.REFERRAL_WITHDRAWAL_SUSPICIOUS_MIN_DEPOSIT_KOPEKS

            for ref_id, (deposit_count, deposit_total) in ref_deposit_map.items():
                ref_user = referrals_by_id.get(ref_id)
                ref_name = ref_user.full_name if ref_user else f'ID{ref_id}'

                suspicious_flags = []

                if deposit_count > max_deposits:
                    analysis['risk_score'] += 15
                    suspicious_flags.append(
                        texts.t('REFERRAL_WITHDRAWAL_FLAG_MANY_DEPOSITS', '{count} пополнений/мес').format(count=deposit_count)
                    )

                if deposit_total > min_suspicious:
                    analysis['risk_score'] += 10
                    suspicious_flags.append(
                        texts.t('REFERRAL_WITHDRAWAL_FLAG_LARGE_AMOUNT', 'сумма {amount} تومان').format(
                            amount=f'{deposit_total / 100:.0f}'
                        )
                    )

                if suspicious_flags:
                    suspicious_referrals.append(
                        {
                            'name': ref_name,
                            'deposits_count': deposit_count,
                            'deposits_total': deposit_total,
                            'flags': suspicious_flags,
                        }
                    )

            analysis['details']['suspicious_referrals'] = suspicious_referrals

            if suspicious_referrals:
                analysis['flags'].append(
                    texts.t('REFERRAL_WITHDRAWAL_FLAG_SUSPICIOUS_REFERRALS', '⚠️ Подозрительная активность у {count} реферала(ов)').format(
                        count=len(suspicious_referrals)
                    )
                )

            # Общая статистика по рефералам (за всё время, только реальные платежи)
            all_ref_deposits = await db.execute(
                select(
                    func.count(func.distinct(Transaction.user_id)).label('paying_count'),
                    func.count().label('total_deposits'),
                    func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('total_amount'),
                ).where(
                    Transaction.user_id.in_(referral_ids),
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed == True,
                    Transaction.payment_method.isnot(None),
                )
            )
            ref_stats = all_ref_deposits.fetchone()
            analysis['details']['referral_deposits'] = {
                'paying_referrals': ref_stats.paying_count,
                'total_deposits': ref_stats.total_deposits,
                'total_amount': ref_stats.total_amount,
            }

            # Проверка: только 1 платящий реферал
            if ref_stats.paying_count == 1 and balance_stats['total_earned'] > 50000:
                analysis['risk_score'] += 20
                analysis['flags'].append(texts.t('REFERRAL_WITHDRAWAL_FLAG_SINGLE_REFERRAL', '⚠️ Весь доход от одного реферала'))

        # 4. Анализ реферальных начислений по типам
        earnings = await db.execute(
            select(
                ReferralEarning.reason,
                func.count().label('count'),
                func.sum(ReferralEarning.amount_kopeks).label('total'),
            )
            .where(ReferralEarning.user_id == user_id)
            .group_by(ReferralEarning.reason)
        )
        earnings_by_reason = {r.reason: {'count': r.count, 'total': r.total} for r in earnings.fetchall()}
        analysis['details']['earnings_by_reason'] = earnings_by_reason

        # 5. Проверка: много начислений за последнюю неделю
        week_ago = datetime.now(UTC) - timedelta(days=7)
        recent_earnings = await db.execute(
            select(func.count(), func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(
                ReferralEarning.user_id == user_id, ReferralEarning.created_at >= week_ago
            )
        )
        recent_data = recent_earnings.fetchone()
        recent_count, recent_amount = recent_data

        if recent_count > 20:
            analysis['risk_score'] += 15
            analysis['flags'].append(
                texts.t('REFERRAL_WITHDRAWAL_FLAG_RECENT_EARNINGS', '⚠️ {count} начислений за неделю ({amount} تومان)').format(
                    count=recent_count, amount=f'{recent_amount / 100:.0f}'
                )
            )

        analysis['details']['recent_activity'] = {
            'week_earnings_count': recent_count,
            'week_earnings_amount': recent_amount,
        }

        # ==================== ИТОГОВАЯ ОЦЕНКА ====================

        score = analysis['risk_score']

        # Ограничиваем максимум
        score = min(score, 100)
        analysis['risk_score'] = score

        if score >= 70:
            analysis['risk_level'] = 'critical'
            analysis['recommendation'] = 'reject'
            analysis['recommendation_text'] = texts.t('REFERRAL_WITHDRAWAL_RECOMMENDATION_REJECT', '🔴 РЕКОМЕНДУЕТСЯ ОТКЛОНИТЬ')
        elif score >= 50:
            analysis['risk_level'] = 'high'
            analysis['recommendation'] = 'review'
            analysis['recommendation_text'] = texts.t('REFERRAL_WITHDRAWAL_RECOMMENDATION_REVIEW', '🟠 ТРЕБУЕТ ПРОВЕРКИ')
        elif score >= 30:
            analysis['risk_level'] = 'medium'
            analysis['recommendation'] = 'review'
            analysis['recommendation_text'] = texts.t('REFERRAL_WITHDRAWAL_RECOMMENDATION_CHECK', '🟡 Рекомендуется проверить')
        else:
            analysis['risk_level'] = 'low'
            analysis['recommendation'] = 'approve'
            analysis['recommendation_text'] = texts.t('REFERRAL_WITHDRAWAL_RECOMMENDATION_APPROVE', '🟢 Можно одобрить')

        return analysis

    # ==================== СОЗДАНИЕ И УПРАВЛЕНИЕ ЗАЯВКАМИ ====================

    async def create_withdrawal_request(
        self, db: AsyncSession, user_id: int, amount_kopeks: int, payment_details: str
    ) -> tuple[WithdrawalRequest | None, str]:
        """
        Создаёт заявку на вывод с анализом на отмывание.
        Возвращает (request, error_message).
        """
        texts = get_texts()
        # Блокируем строку пользователя для предотвращения параллельного создания заявок
        await db.execute(select(User).where(User.id == user_id).with_for_update())

        # Проверяем возможность вывода (stats возвращаются для переиспользования)
        can_request, reason, stats = await self.can_request_withdrawal(db, user_id)
        if not can_request:
            return None, reason

        available = stats['available_total']

        if amount_kopeks > available:
            return None, texts.t('REFERRAL_WITHDRAWAL_NOT_ENOUGH', 'Недостаточно средств. Доступно: {amount} تومان').format(amount=f'{available / 100:.0f}')

        # В режиме "только реф. баланс" проверяем реф. баланс
        if settings.REFERRAL_WITHDRAWAL_ONLY_REFERRAL_BALANCE:
            if amount_kopeks > stats['available_referral']:
                return None, texts.t('REFERRAL_WITHDRAWAL_NOT_ENOUGH_REF', 'Недостаточно реферального баланса. Доступно: {amount} تومان').format(amount=f'{stats["available_referral"] / 100:.0f}')

        # Анализируем на отмывание
        analysis = await self.analyze_for_money_laundering(db, user_id)

        # Создаём заявку
        request = WithdrawalRequest(
            user_id=user_id,
            amount_kopeks=amount_kopeks,
            payment_details=payment_details,
            risk_score=analysis['risk_score'],
            risk_analysis=json.dumps(analysis, ensure_ascii=False, default=str),
        )

        db.add(request)
        await db.commit()
        await db.refresh(request)

        return request, ''

    async def get_pending_requests(self, db: AsyncSession) -> list[WithdrawalRequest]:
        """Получает все ожидающие заявки на вывод."""
        result = await db.execute(
            select(WithdrawalRequest)
            .where(WithdrawalRequest.status == WithdrawalRequestStatus.PENDING.value)
            .order_by(WithdrawalRequest.created_at.asc())
        )
        return result.scalars().all()

    async def get_all_requests(self, db: AsyncSession, limit: int = 50, offset: int = 0) -> list[WithdrawalRequest]:
        """Получает все заявки на вывод (журнал)."""
        result = await db.execute(
            select(WithdrawalRequest).order_by(WithdrawalRequest.created_at.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all()

    async def approve_request(
        self, db: AsyncSession, request_id: int, admin_id: int, comment: str | None = None
    ) -> tuple[bool, str]:
        """
        Одобряет заявку на вывод и списывает средства с баланса.
        Возвращает (success, error_message).
        """
        texts = get_texts()

        result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id).with_for_update())
        request = result.scalar_one_or_none()

        if not request:
            return False, texts.t('REFERRAL_WITHDRAWAL_REQUEST_NOT_FOUND', 'Заявка не найдена')

        if request.status != WithdrawalRequestStatus.PENDING.value:
            return False, texts.t('REFERRAL_WITHDRAWAL_ALREADY_PROCESSED', 'Заявка уже обработана')

        # Получаем пользователя для списания с баланса (с блокировкой строки)
        user_result = await db.execute(select(User).where(User.id == request.user_id).with_for_update())
        user = user_result.scalar_one_or_none()

        if not user:
            return False, texts.t('REFERRAL_WITHDRAWAL_USER_NOT_FOUND', 'Пользователь не найден')

        # Списываем с баланса
        if user.balance_kopeks < request.amount_kopeks:
            return False, texts.t('REFERRAL_WITHDRAWAL_INSUFFICIENT_BALANCE', 'Недостаточно средств на балансе. Баланс: {amount} تومان').format(amount=f'{user.balance_kopeks / 100:.0f}')

        user.balance_kopeks -= request.amount_kopeks

        # Создаём транзакцию списания
        withdrawal_tx = Transaction(
            user_id=request.user_id,
            type=TransactionType.WITHDRAWAL.value,
            amount_kopeks=-request.amount_kopeks,
            description=texts.t('REFERRAL_WITHDRAWAL_TX_DESCRIPTION', 'Вывод реферального баланса (заявка #{id})').format(id=request.id),
            is_completed=True,
            completed_at=datetime.now(UTC),
        )
        db.add(withdrawal_tx)

        # Обновляем статус заявки
        request.status = WithdrawalRequestStatus.APPROVED.value
        request.processed_by = admin_id
        request.processed_at = datetime.now(UTC)
        request.admin_comment = comment

        await db.commit()
        return True, ''

    async def reject_request(
        self, db: AsyncSession, request_id: int, admin_id: int, comment: str | None = None
    ) -> tuple[bool, str]:
        """Отклоняет заявку на вывод."""
        texts = get_texts()

        result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id).with_for_update())
        request = result.scalar_one_or_none()

        if not request:
            return False, texts.t('REFERRAL_WITHDRAWAL_REQUEST_NOT_FOUND', 'Заявка не найдена')

        if request.status != WithdrawalRequestStatus.PENDING.value:
            return False, texts.t('REFERRAL_WITHDRAWAL_ALREADY_PROCESSED', 'Заявка уже обработана')

        request.status = WithdrawalRequestStatus.REJECTED.value
        request.processed_by = admin_id
        request.processed_at = datetime.now(UTC)
        request.admin_comment = comment

        await db.commit()
        return True, ''

    async def complete_request(
        self, db: AsyncSession, request_id: int, admin_id: int, comment: str | None = None
    ) -> tuple[bool, str]:
        """Отмечает заявку как выполненную (деньги переведены)."""
        texts = get_texts()

        result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id).with_for_update())
        request = result.scalar_one_or_none()

        if not request:
            return False, texts.t('REFERRAL_WITHDRAWAL_REQUEST_NOT_FOUND', 'Заявка не найдена')

        if request.status != WithdrawalRequestStatus.APPROVED.value:
            return False, texts.t('REFERRAL_WITHDRAWAL_NOT_APPROVED_STATUS', 'Заявка не в статусе "одобрена"')

        request.status = WithdrawalRequestStatus.COMPLETED.value
        request.processed_by = admin_id
        request.processed_at = datetime.now(UTC)
        if comment:
            request.admin_comment = (request.admin_comment or '') + f'\n{comment}'

        await db.commit()
        return True, ''

    # ==================== ФОРМАТИРОВАНИЕ ====================

    def format_balance_stats_for_user(self, stats: dict, texts) -> str:
        """Форматирует статистику баланса для пользователя."""
        text = ''
        text += (
            texts.t('REFERRAL_WITHDRAWAL_STATS_EARNED', '📈 Всего заработано с рефералов: <b>{amount}</b>').format(
                amount=texts.format_price(stats['total_earned'])
            )
            + '\n'
        )

        text += (
            texts.t('REFERRAL_WITHDRAWAL_STATS_SPENT', '💳 Потрачено на подписки: <b>{amount}</b>').format(
                amount=texts.format_price(stats['referral_spent'])
            )
            + '\n'
        )

        text += (
            texts.t('REFERRAL_WITHDRAWAL_STATS_WITHDRAWN', '💸 Выведено: <b>{amount}</b>').format(
                amount=texts.format_price(stats['withdrawn'])
            )
            + '\n'
        )

        if stats['pending'] > 0:
            text += (
                texts.t('REFERRAL_WITHDRAWAL_STATS_PENDING', '⏳ На рассмотрении: <b>{amount}</b>').format(
                    amount=texts.format_price(stats['pending'])
                )
                + '\n'
            )

        text += '\n'
        text += (
            texts.t('REFERRAL_WITHDRAWAL_STATS_AVAILABLE', '✅ <b>Доступно к выводу: {amount}</b>').format(
                amount=texts.format_price(stats['available_total'])
            )
            + '\n'
        )

        if stats['only_referral_mode']:
            text += (
                texts.t('REFERRAL_WITHDRAWAL_ONLY_REF_MODE', '<i>ℹ️ Выводить можно только реферальный баланс</i>') + '\n'
            )

        return text

    def format_analysis_for_admin(self, analysis: dict) -> str:
        """Форматирует анализ для отображения админу."""
        texts = get_texts()

        risk_emoji = {'low': '🟢', 'medium': '🟡', 'high': '🟠', 'critical': '🔴'}

        text = texts.t(
            'REFERRAL_WITHDRAWAL_ANALYSIS_HEADER',
            '🔍 <b>Анализ на подозрительную активность</b>\n\n'
            '{risk_emoji} Уровень риска: <b>{risk_level}</b>\n'
            '📊 Оценка риска: <b>{risk_score}/100</b>\n'
            '{recommendation}\n',
        ).format(
            risk_emoji=risk_emoji.get(analysis['risk_level'], '⚪'),
            risk_level=analysis['risk_level'].upper(),
            risk_score=analysis['risk_score'],
            recommendation=analysis.get('recommendation_text', ''),
        )

        if analysis.get('flags'):
            text += texts.t('REFERRAL_WITHDRAWAL_ANALYSIS_WARNINGS_HEADER', '\n⚠️ <b>Предупреждения:</b>\n')
            for flag in analysis['flags']:
                text += f'  {flag}\n'

        details = analysis.get('details', {})

        # Статистика баланса
        if 'balance_stats' in details:
            bs = details['balance_stats']
            text += texts.t('REFERRAL_WITHDRAWAL_ANALYSIS_BALANCE_HEADER', '\n💰 <b>Баланс:</b>\n')
            text += texts.t(
                'REFERRAL_WITHDRAWAL_ANALYSIS_BALANCE_EARNED', '• Заработано с рефералов: {amount} تومان\n'
            ).format(amount=f'{bs["total_earned"] / 100:.0f}')
            text += texts.t(
                'REFERRAL_WITHDRAWAL_ANALYSIS_BALANCE_OWN', '• Собственные пополнения: {amount} تومان\n'
            ).format(amount=f'{bs["own_deposits"] / 100:.0f}')
            text += texts.t(
                'REFERRAL_WITHDRAWAL_ANALYSIS_BALANCE_SPENT', '• Потрачено: {amount} تومان\n'
            ).format(amount=f'{bs["spending"] / 100:.0f}')
            text += texts.t(
                'REFERRAL_WITHDRAWAL_ANALYSIS_BALANCE_WITHDRAWN', '• Уже выведено: {amount} تومان\n'
            ).format(amount=f'{bs["withdrawn"] / 100:.0f}')

        # Статистика по рефералам
        if 'referral_deposits' in details:
            rd = details['referral_deposits']
            text += texts.t('REFERRAL_WITHDRAWAL_ANALYSIS_REFERRALS_HEADER', '\n👥 <b>Рефералы:</b>\n')
            text += texts.t('REFERRAL_WITHDRAWAL_ANALYSIS_REFERRALS_TOTAL', '• Всего: {count}\n').format(
                count=details.get('referral_count', 0)
            )
            text += texts.t('REFERRAL_WITHDRAWAL_ANALYSIS_REFERRALS_PAYING', '• Платящих: {count}\n').format(
                count=rd['paying_referrals']
            )
            text += texts.t(
                'REFERRAL_WITHDRAWAL_ANALYSIS_REFERRALS_DEPOSITS',
                '• Всего пополнений: {count} ({amount} تومان)\n',
            ).format(count=rd['total_deposits'], amount=f'{rd["total_amount"] / 100:.0f}')

        # Подозрительные рефералы
        if details.get('suspicious_referrals'):
            text += texts.t('REFERRAL_WITHDRAWAL_ANALYSIS_SUSPICIOUS_HEADER', '\n🚨 <b>Подозрительные рефералы:</b>\n')
            for sr in details['suspicious_referrals'][:5]:
                text += texts.t(
                    'REFERRAL_WITHDRAWAL_ANALYSIS_SUSPICIOUS_ITEM',
                    '• {name}: {count} поп., {amount} تومان\n',
                ).format(
                    name=html.escape(sr['name']),
                    count=sr['deposits_count'],
                    amount=f'{sr["deposits_total"] / 100:.0f}',
                )
                text += texts.t('REFERRAL_WITHDRAWAL_ANALYSIS_SUSPICIOUS_FLAGS', '  Флаги: {flags}\n').format(
                    flags=', '.join(sr['flags'])
                )

        # Источники дохода
        if 'earnings_by_reason' in details:
            text += texts.t('REFERRAL_WITHDRAWAL_ANALYSIS_EARNINGS_HEADER', '\n📊 <b>Источники дохода:</b>\n')
            reason_names = {
                'referral_first_topup': texts.t('REFERRAL_WITHDRAWAL_REASON_FIRST_TOPUP', 'Бонус за 1-е пополнение'),
                'referral_commission_topup': texts.t('REFERRAL_WITHDRAWAL_REASON_COMMISSION_TOPUP', 'Комиссия с пополнений'),
                'referral_commission': texts.t('REFERRAL_WITHDRAWAL_REASON_COMMISSION', 'Комиссия с покупок'),
            }
            for reason, data in details['earnings_by_reason'].items():
                name = reason_names.get(reason, reason)
                text += texts.t(
                    'REFERRAL_WITHDRAWAL_ANALYSIS_EARNINGS_ITEM',
                    '• {name}: {count} шт. ({amount} تومان)\n',
                ).format(name=name, count=data['count'], amount=f'{data["total"] / 100:.0f}')

        return text


# Синглтон сервиса
referral_withdrawal_service = ReferralWithdrawalService()
