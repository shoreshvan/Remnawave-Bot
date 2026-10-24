"""Реферальный вывод: суммы из SQL SUM остаются JSON-сериализуемыми.

PostgreSQL возвращает ``SUM(bigint)`` как ``Decimal``. Эти значения попадали
в FSM aiogram (Redis JSON) и роняли оформление заявки с
``TypeError: Object of type Decimal is not JSON serializable``.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.referral_withdrawal_service import ReferralWithdrawalService


@pytest.mark.asyncio
async def test_balance_stats_are_ints_even_when_sql_sum_returns_decimal(monkeypatch):
    service = ReferralWithdrawalService()

    # PostgreSQL SUM(bigint) -> Decimal; проверяем границу сервиса, а не SQLite.
    monkeypatch.setattr(service, 'get_total_referral_earnings', AsyncMock(return_value=Decimal('150000')))
    monkeypatch.setattr(service, 'get_user_own_deposits', AsyncMock(return_value=Decimal('0')))
    monkeypatch.setattr(service, 'get_user_spending', AsyncMock(return_value=Decimal('0')))
    monkeypatch.setattr(service, 'get_user_spending_after_first_earning', AsyncMock(return_value=Decimal('0')))
    monkeypatch.setattr(service, 'get_withdrawn_amount', AsyncMock(return_value=Decimal('0')))
    monkeypatch.setattr(service, 'get_pending_withdrawal_amount', AsyncMock(return_value=Decimal('0')))

    db = AsyncMock()
    user = MagicMock()
    user.balance_kopeks = 100_000
    db.get.return_value = user

    stats = await service.get_referral_balance_stats(db, 1)

    for key in (
        'total_earned',
        'own_deposits',
        'spending',
        'referral_spent',
        'withdrawn',
        'pending',
        'available_referral',
        'available_total',
    ):
        assert type(stats[key]) is int, f'{key} должен быть int для JSON FSM: {stats[key]!r}'

    assert stats['available_total'] == 100_000
