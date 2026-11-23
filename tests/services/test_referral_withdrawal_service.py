"""Реферальный вывод: суммы из SQL SUM остаются JSON-сериализуемыми.

PostgreSQL возвращает ``SUM(bigint)`` как ``Decimal``. Эти значения попадали
в FSM aiogram (Redis JSON) и роняли оформление заявки с
``TypeError: Object of type Decimal is not JSON serializable``.
После сохранения в ``risk_analysis`` через ``json.dumps(default=str)`` они
превращались в строки и роняли карточку админа на делении ``str / int``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.referral_withdrawal_service import ReferralWithdrawalService, _json_default


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


@pytest.mark.asyncio
async def test_analysis_normalizes_sql_decimal_amounts(monkeypatch):
    service = ReferralWithdrawalService()
    monkeypatch.setattr(
        service,
        'get_referral_balance_stats',
        AsyncMock(
            return_value={
                'total_earned': 100_000,
                'own_deposits': 0,
                'spending': 0,
                'referral_spent': 0,
                'withdrawn': 0,
                'pending': 0,
                'available_referral': 100_000,
                'available_total': 100_000,
                'only_referral_mode': True,
            }
        ),
    )

    referrals_result = MagicMock()
    referrals_result.scalars.return_value.all.return_value = [
        SimpleNamespace(id=2, full_name='Referral'),
    ]

    ref_deposits_result = MagicMock()
    ref_deposits_result.all.return_value = [
        SimpleNamespace(user_id=2, count=2, total=Decimal('100000.00')),
    ]

    all_ref_deposits_result = MagicMock()
    all_ref_deposits_result.fetchone.return_value = SimpleNamespace(
        paying_count=1,
        total_deposits=2,
        total_amount=Decimal('100000.00'),
    )

    earnings_result = MagicMock()
    earnings_result.fetchall.return_value = [
        SimpleNamespace(reason='referral_commission_topup', count=2, total=Decimal('10000.00')),
    ]

    recent_result = MagicMock()
    recent_result.fetchone.return_value = (Decimal('3'), Decimal('30000.00'))

    db = AsyncMock()
    db.execute.side_effect = [
        referrals_result,
        ref_deposits_result,
        all_ref_deposits_result,
        earnings_result,
        recent_result,
    ]

    analysis = await service.analyze_for_money_laundering(db, 1)
    details = analysis['details']

    assert details['referral_deposits']['total_amount'] == 100_000
    assert type(details['referral_deposits']['total_amount']) is int
    assert details['earnings_by_reason']['referral_commission_topup']['total'] == 10_000
    assert type(details['earnings_by_reason']['referral_commission_topup']['total']) is int
    assert details['recent_activity']['week_earnings_count'] == 3
    assert type(details['recent_activity']['week_earnings_count']) is int
    assert details['recent_activity']['week_earnings_amount'] == 30_000
    assert type(details['recent_activity']['week_earnings_amount']) is int


def test_format_analysis_accepts_legacy_string_amounts():
    analysis = {
        'risk_score': 40,
        'risk_level': 'medium',
        'recommendation_text': 'review',
        'flags': ['notice'],
        'details': {
            'balance_stats': {
                'total_earned': '150000.00',
                'own_deposits': '0',
                'spending': '0',
                'withdrawn': '0',
            },
            'referral_count': 2,
            'referral_deposits': {
                'paying_referrals': 1,
                'total_deposits': 2,
                'total_amount': '100000.00',
            },
            'suspicious_referrals': [
                {
                    'name': 'Referral',
                    'deposits_count': 2,
                    'deposits_total': '50000.00',
                    'flags': ['flag'],
                }
            ],
            'earnings_by_reason': {
                'referral_commission_topup': {'count': 2, 'total': '10000.00'},
            },
        },
    }

    text = ReferralWithdrawalService().format_analysis_for_admin(analysis)

    assert '1500' in text
    assert '1000' in text
    assert '500' in text
    assert '100' in text


def test_format_analysis_tolerates_empty_or_broken_payload():
    text = ReferralWithdrawalService().format_analysis_for_admin({})
    assert '0/100' in text

    text = ReferralWithdrawalService().format_analysis_for_admin({'details': None, 'flags': None})
    assert '0/100' in text


def test_json_default_keeps_decimal_amounts_numeric():
    payload = json.loads(json.dumps({'total': Decimal('100000.00')}, default=_json_default))

    assert payload['total'] == 100_000
    assert type(payload['total']) is int


@pytest.mark.asyncio
async def test_create_rejects_non_positive_amount(monkeypatch):
    service = ReferralWithdrawalService()
    monkeypatch.setattr(
        service,
        'can_request_withdrawal',
        AsyncMock(
            return_value=(
                True,
                'OK',
                {
                    'available_total': 100_000,
                    'available_referral': 100_000,
                },
            )
        ),
    )
    db = AsyncMock()

    request, error = await service.create_withdrawal_request(db, 1, 0, 'card 1234')

    assert request is None
    assert error
    db.add.assert_not_called()
