"""create tonpays_payments table

Revision ID: 0134
Revises: 0133
Create Date: 2026-10-06

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0134'
down_revision: Union[str, None] = '0133'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tonpays_payments',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True),
        sa.Column('order_id', sa.String(20), unique=True, nullable=False, index=True),
        sa.Column('tonpays_payment_id', sa.String(64), unique=True, nullable=True, index=True),
        sa.Column('amount_kopeks', sa.BigInteger(), nullable=False),
        sa.Column('final_amount_kopeks', sa.BigInteger(), nullable=True),
        sa.Column('credit_amount_kopeks', sa.BigInteger(), nullable=True),
        sa.Column('currency', sa.String(10), nullable=False, server_default='IRT'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('card_number', sa.String(64), nullable=True),
        sa.Column('card_name', sa.String(255), nullable=True),
        sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('is_paid', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('receipt_received', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('last_card_change_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('callback_payload', sa.JSON(), nullable=True),
        sa.Column('processed_events', sa.JSON(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
    )
    # The model declares id as index=True — without this index an upgraded DB
    # would differ from a fresh one built by create_all.
    op.create_index('ix_tonpays_payments_id', 'tonpays_payments', ['id'])


def downgrade() -> None:
    op.drop_table('tonpays_payments')
