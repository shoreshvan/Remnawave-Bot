"""merge fork money-columns branch (0111a) into upstream v5 chain (0131)

Two revisions branch off 0110: upstream's 0111 continues linearly through the
v5.0.0 additions (dpichecker/broadcast/cashera) to 0131, and the fork's 0111a
(money_columns_bigint). That left the tree with two heads, so `alembic upgrade
head` would fail with "Multiple head revisions are present". This empty merge
revision joins both heads into a single head; it performs no schema changes of
its own.

Revision ID: 0132
Revises: 0111a, 0131
"""

from typing import Sequence, Union


revision: str = '0132'
down_revision: Union[str, Sequence[str], None] = ('0111a', '0131')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
