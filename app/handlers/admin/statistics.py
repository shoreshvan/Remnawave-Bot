from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import get_referral_statistics
from app.database.crud.subscription import get_subscriptions_statistics
from app.database.crud.transaction import get_revenue_by_period, get_transactions_statistics
from app.database.models import User
from app.keyboards.admin import get_admin_statistics_keyboard
from app.localization.texts import get_texts
from app.services.referral_reward_service import format_reward_total
from app.services.user_service import UserService
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime, format_percentage


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_statistics_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    text = texts.t(
        'ADMIN_STATS_MENU',
        """
📊 <b>Статистика системы</b>

Выберите раздел для просмотра статистики:
""",
    )

    await callback.message.edit_text(text, reply_markup=get_admin_statistics_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_users_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_service = UserService()
    stats = await user_service.get_user_statistics(db)

    total_users = stats['total_users']
    active_rate = format_percentage(stats['active_users'] / total_users * 100 if total_users > 0 else 0)

    current_time = format_datetime(datetime.now(UTC))

    text = texts.t(
        'ADMIN_STATS_USERS_BODY',
        """
👥 <b>Статистика пользователей</b>

<b>Общие показатели:</b>
- Всего зарегистрировано: {total_users}
- Активных: {active_users} ({active_rate})
- Заблокированных: {blocked_users}

<b>Новые регистрации:</b>
- Сегодня: {new_today}
- За неделю: {new_week}
- За месяц: {new_month}

<b>Активность:</b>
- Коэффициент активности: {active_rate}
- Рост за месяц: +{new_month} ({month_growth_rate})

<b>Обновлено:</b> {current_time}
""",
    ).format(
        total_users=stats['total_users'],
        active_users=stats['active_users'],
        active_rate=active_rate,
        blocked_users=stats['blocked_users'],
        new_today=stats['new_today'],
        new_week=stats['new_week'],
        new_month=stats['new_month'],
        month_growth_rate=format_percentage(stats['new_month'] / total_users * 100 if total_users > 0 else 0),
        current_time=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_STATS_REFRESH_BUTTON', '🔄 Обновить'),
                    callback_data='admin_stats_users',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_STATS_BACK_BUTTON', '⬅️ Назад'),
                    callback_data='admin_statistics',
                )
            ],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_UP_TO_DATE_TOAST', '📊 Данные актуальны'), show_alert=False)
        else:
            logger.error('Ошибка обновления статистики пользователей', error=e)
            await callback.answer(
                texts.t('ADMIN_STATS_UPDATE_ERROR_TOAST', '❌ Ошибка обновления данных'), show_alert=True
            )
            return

    await callback.answer(texts.t('ADMIN_STATS_UPDATED_TOAST', '✅ Статистика обновлена'))


@admin_required
@error_handler
async def show_subscriptions_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_subscriptions_statistics(db)

    total_subs = stats['total_subscriptions']
    conversion_rate = format_percentage(stats['paid_subscriptions'] / total_subs * 100 if total_subs > 0 else 0)
    current_time = format_datetime(datetime.now(UTC))

    text = texts.t(
        'ADMIN_STATS_SUBS_BODY',
        """
📱 <b>Статистика подписок</b>

<b>Общие показатели:</b>
- Всего подписок: {total_subscriptions}
- Активных: {active_subscriptions}
- Платных: {paid_subscriptions}
- Триальных: {trial_subscriptions}

<b>Конверсия:</b>
- Из триала в платную: {conversion_rate}
- Активных платных: {paid_subscriptions}

<b>Продажи:</b>
- Сегодня: {purchased_today}
- За неделю: {purchased_week}
- За месяц: {purchased_month}

<b>Обновлено:</b> {current_time}
""",
    ).format(
        total_subscriptions=stats['total_subscriptions'],
        active_subscriptions=stats['active_subscriptions'],
        paid_subscriptions=stats['paid_subscriptions'],
        trial_subscriptions=stats['trial_subscriptions'],
        conversion_rate=conversion_rate,
        purchased_today=stats['purchased_today'],
        purchased_week=stats['purchased_week'],
        purchased_month=stats['purchased_month'],
        current_time=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_STATS_REFRESH_BUTTON', '🔄 Обновить'),
                    callback_data='admin_stats_subs',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_STATS_BACK_BUTTON', '⬅️ Назад'),
                    callback_data='admin_statistics',
                )
            ],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATS_UPDATED_TOAST', '✅ Статистика обновлена'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_UP_TO_DATE_TOAST', '📊 Данные актуальны'), show_alert=False)
        else:
            logger.error('Ошибка обновления статистики подписок', error=e)
            await callback.answer(
                texts.t('ADMIN_STATS_UPDATE_ERROR_TOAST', '❌ Ошибка обновления данных'), show_alert=True
            )


@admin_required
@error_handler
async def show_revenue_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    month_stats = await get_transactions_statistics(db, month_start, now)
    all_time_stats = await get_transactions_statistics(db, start_date=datetime(2020, 1, 1, tzinfo=UTC), end_date=now)
    current_time = format_datetime(datetime.now(UTC))

    text = texts.t(
        'ADMIN_STATS_REVENUE_BODY',
        """
💰 <b>Статистика доходов</b>

<b>За текущий месяц:</b>
- Доходы: {month_income}
- Расходы: {month_expenses}
- Прибыль: {month_profit}
- От подписок: {month_subscription_income}

<b>Сегодня:</b>
- Транзакций: {today_transactions}
- Доходы: {today_income}

<b>За все время:</b>
- Общий доход: {all_time_income}
- Общая прибыль: {all_time_profit}

<b>Способы оплаты:</b>
""",
    ).format(
        month_income=settings.format_price(month_stats['totals']['income_kopeks']),
        month_expenses=settings.format_price(month_stats['totals']['expenses_kopeks']),
        month_profit=settings.format_price(month_stats['totals']['profit_kopeks']),
        month_subscription_income=settings.format_price(abs(month_stats['totals']['subscription_income_kopeks'])),
        today_transactions=month_stats['today']['transactions_count'],
        today_income=settings.format_price(month_stats['today']['income_kopeks']),
        all_time_income=settings.format_price(all_time_stats['totals']['income_kopeks']),
        all_time_profit=settings.format_price(all_time_stats['totals']['profit_kopeks']),
    )

    for method, data in month_stats['by_payment_method'].items():
        if method and data['count'] > 0:
            text += f'• {method}: {data["count"]} ({settings.format_price(data["amount"])})\n'

    text += texts.t('ADMIN_STATS_UPDATED', '\n<b>Обновлено:</b> {current_time}').format(current_time=current_time)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            # [types.InlineKeyboardButton(text="📈 Период", callback_data="admin_revenue_period")],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_STATS_REFRESH_BUTTON', '🔄 Обновить'),
                    callback_data='admin_stats_revenue',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_STATS_BACK_BUTTON', '⬅️ Назад'),
                    callback_data='admin_statistics',
                )
            ],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATS_UPDATED_TOAST', '✅ Статистика обновлена'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_UP_TO_DATE_TOAST', '📊 Данные актуальны'), show_alert=False)
        else:
            logger.error('Ошибка обновления статистики доходов', error=e)
            await callback.answer(
                texts.t('ADMIN_STATS_UPDATE_ERROR_TOAST', '❌ Ошибка обновления данных'), show_alert=True
            )


@admin_required
@error_handler
async def show_referral_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_referral_statistics(db)
    current_time = format_datetime(datetime.now(UTC))

    avg_per_referrer = 0
    if stats['active_referrers'] > 0:
        avg_per_referrer = stats['total_paid_kopeks'] / stats['active_referrers']

    # Дни — вторая валюта программы: без них экран показывает «выплачено 0 ₽»
    # на установке, где начисления идут днями подписки.
    text = texts.t(
        'ADMIN_STATS_REFERRAL_BODY',
        """
🤝 <b>Реферальная статистика</b>

<b>Общие показатели:</b>
- Пользователей с рефералами: {users_with_referrals}
- Активных рефереров: {active_referrers}
- Выплачено всего: {total_paid}

<b>За период:</b>
- Сегодня: {today_earnings}
- За неделю: {week_earnings}
- За месяц: {month_earnings}

<b>Средние показатели:</b>
- На одного рефререра: {avg_per_referrer}
""",
    ).format(
        users_with_referrals=stats['users_with_referrals'],
        active_referrers=stats['active_referrers'],
        total_paid=format_reward_total(stats['total_paid_kopeks'], stats.get('total_paid_days', 0)),
        today_earnings=format_reward_total(stats['today_earnings_kopeks'], stats.get('today_earnings_days', 0)),
        week_earnings=format_reward_total(stats['week_earnings_kopeks'], stats.get('week_earnings_days', 0)),
        month_earnings=format_reward_total(stats['month_earnings_kopeks'], stats.get('month_earnings_days', 0)),
        avg_per_referrer=settings.format_price(int(avg_per_referrer)),
    )

    meaningful_levels = [row for row in (stats.get('by_level') or []) if row.get('money_kopeks') or row.get('days')]
    if len(meaningful_levels) > 1:
        text += texts.t('ADMIN_STATS_REFERRAL_BY_LEVELS', '\n<b>По уровням:</b>\n')
        for row in meaningful_levels:
            text += texts.t('ADMIN_STATS_REFERRAL_LEVEL_ROW', '- Уровень {level}: {reward}\n').format(
                level=row['level'],
                reward=format_reward_total(row.get('money_kopeks', 0), row.get('days', 0)),
            )

    text += texts.t('ADMIN_STATS_REFERRAL_TOP', '\n<b>Топ рефереры:</b>\n')

    if stats['top_referrers']:
        for i, referrer in enumerate(stats['top_referrers'][:5], 1):
            name = referrer['display_name']
            earned = format_reward_total(referrer['total_earned_kopeks'], referrer.get('total_earned_days', 0))
            count = referrer['referrals_count']
            text += texts.t('ADMIN_STATS_REFERRAL_TOP_ROW', '{position}. {name}: {earned} ({count} реф.)\n').format(
                position=i, name=name, earned=earned, count=count
            )
    else:
        text += texts.t('ADMIN_STATS_REFERRAL_NO_REFERRERS', 'Пока нет активных рефереров')

    text += texts.t('ADMIN_STATS_UPDATED', '\n<b>Обновлено:</b> {current_time}').format(current_time=current_time)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_STATS_REFRESH_BUTTON', '🔄 Обновить'),
                    callback_data='admin_stats_referrals',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_STATS_BACK_BUTTON', '⬅️ Назад'),
                    callback_data='admin_statistics',
                )
            ],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATS_UPDATED_TOAST', '✅ Статистика обновлена'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_UP_TO_DATE_TOAST', '📊 Данные актуальны'), show_alert=False)
        else:
            logger.error('Ошибка обновления реферальной статистики', error=e)
            await callback.answer(
                texts.t('ADMIN_STATS_UPDATE_ERROR_TOAST', '❌ Ошибка обновления данных'), show_alert=True
            )


@admin_required
@error_handler
async def show_summary_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_service = UserService()
    user_stats = await user_service.get_user_statistics(db)
    sub_stats = await get_subscriptions_statistics(db)

    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    revenue_stats = await get_transactions_statistics(db, month_start, now)
    current_time = format_datetime(datetime.now(UTC))

    conversion_rate = 0
    if user_stats['total_users'] > 0:
        conversion_rate = sub_stats['paid_subscriptions'] / user_stats['total_users'] * 100

    arpu = 0
    if user_stats['active_users'] > 0:
        arpu = revenue_stats['totals']['income_kopeks'] / user_stats['active_users']

    text = texts.t(
        'ADMIN_STATS_SUMMARY_BODY',
        """
📊 <b>Общая сводка системы</b>

<b>Пользователи:</b>
- Всего: {total_users}
- Активных: {active_users}
- Новых за месяц: {new_month}

<b>Подписки:</b>
- Активных: {active_subscriptions}
- Платных: {paid_subscriptions}
- Конверсия: {conversion_rate}

<b>Финансы (месяц):</b>
- Доходы: {income}
- ARPU: {arpu}
- Транзакций: {transactions_count}

<b>Рост:</b>
- Пользователи: +{new_month} за месяц
- Продажи: +{purchased_month} за месяц

<b>Обновлено:</b> {current_time}
""",
    ).format(
        total_users=user_stats['total_users'],
        active_users=user_stats['active_users'],
        new_month=user_stats['new_month'],
        active_subscriptions=sub_stats['active_subscriptions'],
        paid_subscriptions=sub_stats['paid_subscriptions'],
        conversion_rate=format_percentage(conversion_rate),
        income=settings.format_price(revenue_stats['totals']['income_kopeks']),
        arpu=settings.format_price(int(arpu)),
        transactions_count=sum(data['count'] for data in revenue_stats['by_type'].values()),
        purchased_month=sub_stats['purchased_month'],
        current_time=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_STATS_REFRESH_BUTTON', '🔄 Обновить'),
                    callback_data='admin_stats_summary',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_STATS_BACK_BUTTON', '⬅️ Назад'),
                    callback_data='admin_statistics',
                )
            ],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATS_UPDATED_TOAST', '✅ Статистика обновлена'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_UP_TO_DATE_TOAST', '📊 Данные актуальны'), show_alert=False)
        else:
            logger.error('Ошибка обновления общей статистики', error=e)
            await callback.answer(
                texts.t('ADMIN_STATS_UPDATE_ERROR_TOAST', '❌ Ошибка обновления данных'), show_alert=True
            )


@admin_required
@error_handler
async def show_revenue_by_period(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    period = callback.data.split('_')[-1]

    period_map = {'today': 1, 'yesterday': 1, 'week': 7, 'month': 30, 'all': 365}

    days = period_map.get(period, 30)
    revenue_data = await get_revenue_by_period(db, days)

    if period == 'yesterday':
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
        revenue_data = [r for r in revenue_data if r['date'] == yesterday]
    elif period == 'today':
        today = datetime.now(UTC).date()
        revenue_data = [r for r in revenue_data if r['date'] == today]

    total_revenue = sum(r['amount_kopeks'] for r in revenue_data)
    avg_daily = total_revenue / len(revenue_data) if revenue_data else 0

    text = texts.t(
        'ADMIN_STATS_REVENUE_PERIOD_BODY',
        """
📈 <b>Доходы за период: {period}</b>

<b>Сводка:</b>
- Общий доход: {total_revenue}
- Дней с данными: {days_with_data}
- Средний доход в день: {avg_daily}

<b>По дням:</b>
""",
    ).format(
        period=period,
        total_revenue=settings.format_price(total_revenue),
        days_with_data=len(revenue_data),
        avg_daily=settings.format_price(int(avg_daily)),
    )

    for revenue in revenue_data[-10:]:
        text += f'• {revenue["date"].strftime("%d.%m")}: {settings.format_price(revenue["amount_kopeks"])}\n'

    if len(revenue_data) > 10:
        text += texts.t('ADMIN_STATS_REVENUE_PERIOD_MORE', '... и еще {count} дней').format(
            count=len(revenue_data) - 10
        )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_STATS_REVENUE_PERIOD_OTHER_BUTTON', '📊 Другой период'),
                        callback_data='admin_revenue_period',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_STATS_REVENUE_PERIOD_BACK_BUTTON', '⬅️ К доходам'),
                        callback_data='admin_stats_revenue',
                    )
                ],
            ]
        ),
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_statistics_menu, F.data == 'admin_statistics')
    dp.callback_query.register(show_users_statistics, F.data == 'admin_stats_users')
    dp.callback_query.register(show_subscriptions_statistics, F.data == 'admin_stats_subs')
    dp.callback_query.register(show_revenue_statistics, F.data == 'admin_stats_revenue')
    dp.callback_query.register(show_referral_statistics, F.data == 'admin_stats_referrals')
    dp.callback_query.register(show_summary_statistics, F.data == 'admin_stats_summary')
    dp.callback_query.register(show_revenue_by_period, F.data.startswith('period_'))

    periods = ['today', 'yesterday', 'week', 'month', 'all']
    for period in periods:
        dp.callback_query.register(show_revenue_by_period, F.data == f'period_{period}')
