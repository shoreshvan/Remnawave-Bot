"""convert money (kopeks) columns from Integer to BigInteger

Revision ID: 0111a
Revises: 0110

Integer (int32) tops out at 2,147,483,647 minor units, capping balances and
payment amounts at ~21.5M major units — too low for Toman-denominated
deployments (a 25,000,000 Toman top-up would overflow). BigInteger (int64)
removes that ceiling. No data conversion needed: existing values are intact.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0111a'
down_revision: Union[str, None] = '0110'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table_name, column_name, nullable) — mirrors models.py after Phase 1.
MONEY_COLUMNS = [
    ('yookassa_payments', 'amount_kopeks', False),
    ('apple_transactions', 'amount_kopeks', False),
    ('mulenpay_payments', 'amount_kopeks', False),
    ('pal24_payments', 'amount_kopeks', False),
    ('wata_payments', 'amount_kopeks', False),
    ('platega_payments', 'amount_kopeks', False),
    ('platega_subscriptions', 'amount_kopeks', False),
    ('lava_subscriptions', 'amount_kopeks', False),
    ('cloudpayments_payments', 'amount_kopeks', False),
    ('freekassa_payments', 'amount_kopeks', False),
    ('kassa_ai_payments', 'amount_kopeks', False),
    ('riopay_payments', 'amount_kopeks', False),
    ('severpay_payments', 'amount_kopeks', False),
    ('paypear_payments', 'amount_kopeks', False),
    ('rollypay_payments', 'amount_kopeks', False),
    ('overpay_payments', 'amount_kopeks', False),
    ('aurapay_payments', 'amount_kopeks', False),
    ('etoplatezhi_payments', 'amount_kopeks', False),
    ('antilopay_payments', 'amount_kopeks', False),
    ('jupiter_payments', 'amount_kopeks', False),
    ('donut_payments', 'amount_kopeks', False),
    ('lava_payments', 'amount_kopeks', False),
    ('cispay_payments', 'amount_kopeks', False),
    ('cispay_payments', 'charged_amount_kopeks', True),
    ('promo_groups', 'auto_assign_total_spent_kopeks', True),
    ('tariffs', 'device_price_kopeks', True),
    ('tariffs', 'daily_price_kopeks', False),
    ('tariffs', 'price_per_day_kopeks', False),
    ('tariffs', 'traffic_price_per_gb_kopeks', False),
    ('users', 'balance_kopeks', True),
    ('users', 'auto_promo_group_threshold_kopeks', False),
    ('transactions', 'amount_kopeks', False),
    ('subscription_conversions', 'first_payment_amount_kopeks', True),
    ('promocodes', 'balance_bonus_kopeks', True),
    ('coupon_batches', 'wholesale_price_kopeks', False),
    ('referral_reward_levels', 'referrer_fixed_kopeks', True),
    ('referral_reward_levels', 'referee_fixed_kopeks', True),
    ('referral_earnings', 'amount_kopeks', False),
    ('withdrawal_requests', 'amount_kopeks', False),
    ('referral_contest_events', 'amount_kopeks', False),
    ('referral_contest_virtual_participants', 'total_amount_kopeks', False),
    ('squads', 'price_kopeks', True),
    ('subscription_events', 'amount_kopeks', True),
    ('discount_offers', 'bonus_amount_kopeks', False),
    ('promo_offer_templates', 'bonus_amount_kopeks', False),
    ('polls', 'reward_amount_kopeks', False),
    ('poll_responses', 'reward_amount_kopeks', False),
    ('server_squads', 'price_kopeks', True),
    ('subscription_servers', 'paid_price_kopeks', True),
    ('advertising_campaigns', 'balance_bonus_kopeks', True),
    ('advertising_campaign_registrations', 'balance_bonus_kopeks', True),
    ('wheel_prizes', 'prize_value_kopeks', False),
    ('wheel_prizes', 'promo_balance_bonus_kopeks', True),
    ('wheel_spins', 'payment_value_kopeks', False),
    ('wheel_spins', 'prize_value_kopeks', False),
    ('payment_method_configs', 'min_amount_kopeks', True),
    ('payment_method_configs', 'max_amount_kopeks', True),
    ('guest_purchases', 'amount_kopeks', False),
]


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    return insp.has_table(table_name)


def _is_already_bigint(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    for col in insp.get_columns(table_name):
        if col['name'] == column_name:
            return isinstance(col['type'], sa.BigInteger)
    return True  # column missing — nothing to do


def _alter(table_name: str, column_name: str, nullable: bool, to_bigint: bool) -> None:
    new_type = sa.BigInteger() if to_bigint else sa.Integer()
    old_type = sa.Integer() if to_bigint else sa.BigInteger()
    if op.get_bind().dialect.name == 'sqlite':
        # SQLite has no ALTER COLUMN — recreate the table via batch mode.
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                column_name,
                existing_type=old_type,
                type_=new_type,
                existing_nullable=nullable,
            )
    else:
        op.alter_column(
            table_name,
            column_name,
            existing_type=old_type,
            type_=new_type,
            existing_nullable=nullable,
        )


def upgrade() -> None:
    for table_name, column_name, nullable in MONEY_COLUMNS:
        if not _table_exists(table_name):
            continue
        if _is_already_bigint(table_name, column_name):
            continue
        _alter(table_name, column_name, nullable, to_bigint=True)


def downgrade() -> None:
    # WARNING: fails if any stored value exceeds the int32 range (2,147,483,647).
    for table_name, column_name, nullable in MONEY_COLUMNS:
        if not _table_exists(table_name):
            continue
        if not _is_already_bigint(table_name, column_name):
            continue
        _alter(table_name, column_name, nullable, to_bigint=False)
