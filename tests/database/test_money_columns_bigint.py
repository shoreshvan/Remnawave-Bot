"""Phase 1 (toman localization): money columns must be BigInteger.

Two guarantees are pinned here:
1. Every kopeks-denominated column in ``app.database.models`` is declared as
   ``BigInteger`` so balances/payments are not capped at int32
   (~21.5M major units).
2. Migration 0111 converts a legacy INTEGER column to BIGINT preserving data
   and nullability, and is safe to run twice (idempotent).
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / 'migrations' / 'alembic' / 'versions' / '0111_money_columns_bigint.py'


def _load_migration():
    spec = importlib.util.spec_from_file_location('m0111', MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestModelColumnTypes:
    def test_all_money_columns_are_bigint(self):
        migration = _load_migration()
        assert len(migration.MONEY_COLUMNS) == 58

        tables = Base.metadata.tables
        wrong = []
        for table_name, column_name, _nullable in migration.MONEY_COLUMNS:
            table = tables.get(table_name)
            if table is None:
                wrong.append(f'{table_name}: table missing')
                continue
            column = table.columns.get(column_name)
            if column is None:
                wrong.append(f'{table_name}.{column_name}: column missing')
                continue
            if not isinstance(column.type, sa.BigInteger):
                wrong.append(f'{table_name}.{column_name}: {column.type} is not BigInteger')
        assert not wrong, 'columns still not BigInteger:\n' + '\n'.join(wrong)

    def test_models_match_migration_nullability(self):
        migration = _load_migration()
        tables = Base.metadata.tables
        mismatches = []
        for table_name, column_name, nullable in migration.MONEY_COLUMNS:
            column = tables[table_name].columns[column_name]
            if column.nullable != nullable:
                mismatches.append(f'{table_name}.{column_name}: model={column.nullable} migration={nullable}')
        assert not mismatches, 'nullability mismatch:\n' + '\n'.join(mismatches)


class TestMigration0111:
    def test_upgrade_converts_integer_to_bigint_preserving_data(self, tmp_path):
        db_path = tmp_path / 'm0111.db'
        engine = sa.create_engine(f'sqlite:///{db_path}')
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY, balance_kopeks INTEGER NOT NULL DEFAULT 0)')
                )
                conn.execute(
                    sa.text(
                        'CREATE TABLE tariffs (id INTEGER PRIMARY KEY, '
                        'device_price_kopeks INTEGER, '
                        'daily_price_kopeks INTEGER NOT NULL DEFAULT 0)'
                    )
                )
                # larger than int32 max (2,147,483,647): 50,000,000 toman in minor units
                big_value = 5_000_000_000
                conn.execute(sa.text('INSERT INTO users (id, balance_kopeks) VALUES (1, :v)'), {'v': big_value})

            migration = _load_migration()
            with engine.begin() as conn:
                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    migration.upgrade()

            with engine.connect() as conn:
                insp = sa.inspect(conn)
                for table, col in [
                    ('users', 'balance_kopeks'),
                    ('tariffs', 'device_price_kopeks'),
                    ('tariffs', 'daily_price_kopeks'),
                ]:
                    coltype = {c['name']: c['type'] for c in insp.get_columns(table)}[col]
                    assert isinstance(coltype, sa.BigInteger), f'{table}.{col} not converted'

                value = conn.execute(sa.text('SELECT balance_kopeks FROM users WHERE id=1')).scalar()
                assert value == 5_000_000_000, 'data lost during conversion'

                cols = {c['name']: c for c in insp.get_columns('tariffs')}
                assert cols['device_price_kopeks']['nullable'] is True
                assert cols['daily_price_kopeks']['nullable'] is False

            # idempotency: a second run must be a no-op
            with engine.begin() as conn:
                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    migration.upgrade()
        finally:
            engine.dispose()
