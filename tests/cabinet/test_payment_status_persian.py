"""Execute cabinet payment functions in isolation from optional web dependencies."""

import ast
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, status
from sqlalchemy import create_engine, desc, select

from app.database.models import PaymentMethod
from app.localization.texts import get_texts
from app.services.payment_verification_service import PendingPayment
from tests.fixtures.persian import persian  # noqa: F401


@pytest.fixture
def payment_functions():
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/balance.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names = {'_get_status_info', '_get_status_info_legacy', '_record_to_response',
             '_get_payment_url', '_is_checkable', 'check_payment_status', 'get_pending_payments',
             'get_pending_payment_details', 'get_latest_payment_by_method'}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in names]
    assert len(nodes) == len(names)
    for node in nodes:
        node.decorator_list = []
        node.args.defaults = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                             *nodes], type_ignores=[])
    context = dict(math=math, select=select, desc=desc, PendingPayment=PendingPayment,
                   PendingPaymentListResponse=SimpleNamespace,
                   PaymentMethod=PaymentMethod, get_texts=get_texts, HTTPException=HTTPException, status=status,
                   PendingPaymentResponse=SimpleNamespace, ManualCheckResponse=SimpleNamespace,
                   SUPPORTED_MANUAL_CHECK_METHODS=set(PaymentMethod), method_display_name=lambda method: method.value)
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), context)
    return context


def record(provider, state='pending', paid=False):
    return SimpleNamespace(method=PaymentMethod(provider), status=state, is_paid=paid,
                           payment=SimpleNamespace(payment_url='https://example.test/pay'),
                           user=SimpleNamespace(id=123, language='fa', telegram_id=456, username='test'),
                           local_id=7, identifier='invoice-7', amount_kopeks=250000,
                           created_at=None, expires_at=None, is_recent=lambda: True)


@pytest.mark.parametrize('provider', ['atlaspay', 'hooshpay', 'tonpays', 'nowpayments'])
@pytest.mark.parametrize('state', ['pending', 'processing', 'success', 'expired', 'cancelled', 'failed',
                                  'declined', 'error', 'amount_mismatch', 'need_action', 'underpaid_review',
                                  'manual_review', 'rejected', 'refunded', 'unexpected'])
def test_status_and_response_are_persian(persian, payment_functions, provider, state):
    item = record(provider, state)
    result = payment_functions['_record_to_response'](item)
    assert any('\u0600' <= char <= '\u06ff' for char in result.status_text)
    assert not any('\u0400' <= char <= '\u04ff' for char in result.status_text)
    assert result.status == state and result.amount_kopeks == 250000 and result.amount_rubles == 2500
    assert result.identifier == 'invoice-7' and result.payment_url == item.payment.payment_url
    if state == 'amount_mismatch':
        assert result.status_text == 'مغایرت مبلغ'
    if state == 'unexpected':
        assert result.status_text == 'وضعیت نامشخص'
    item.is_paid = True
    assert payment_functions['_get_status_info'](item) == ('✅', persian.t('ADMIN_PAYMENT_STATUS_PAID'))


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['atlaspay', 'hooshpay', 'tonpays', 'nowpayments'])
@pytest.mark.parametrize('outcome', ['unavailable', 'failed', 'unchanged', 'changed'])
async def test_manual_check_messages(persian, payment_functions, provider, outcome):
    item = record(provider, 'expired' if outcome == 'unavailable' else 'pending')
    updated = record(provider, 'success', True) if outcome == 'changed' else record(provider)
    checker = AsyncMock(return_value=None if outcome == 'failed' else updated)
    close = AsyncMock()
    payment_functions.update(get_payment_record=AsyncMock(return_value=item), run_manual_check=checker,
                             create_bot=lambda: SimpleNamespace(session=SimpleNamespace(close=close)),
                             PaymentService=lambda **kwargs: object())
    db = object()
    result = await payment_functions['check_payment_status'](provider, 7, item.user, db)
    keys = {'unavailable': 'CABINET_PAYMENT_CHECK_UNAVAILABLE', 'failed': 'CABINET_PAYMENT_CHECK_FAILED',
            'unchanged': 'CABINET_PAYMENT_STATUS_UNCHANGED', 'changed': 'CABINET_PAYMENT_STATUS_UPDATED'}
    assert result.message == persian.t(keys[outcome]).format(status=persian.t('ADMIN_PAYMENT_STATUS_PAID'))
    assert result.payment.status_text == persian.t('ADMIN_PAYMENT_STATUS_PAID' if outcome == 'changed'
                                                  else 'ADMIN_PAYMENT_STATUS_EXPIRED' if outcome == 'unavailable'
                                                  else 'ADMIN_PAYMENT_STATUS_PENDING')
    assert result.status_changed == (outcome == 'changed')
    if outcome == 'unavailable':
        checker.assert_not_awaited()
        close.assert_not_awaited()
    else:
        assert checker.await_args.args[:3] == (db, PaymentMethod(provider), 7)
        close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('invalid', 400, 'CABINET_PAYMENT_INVALID_METHOD'),
    ('missing', 404, 'CABINET_BALANCE_PAYMENT_NOT_FOUND'),
    ('forbidden', 403, 'CABINET_BALANCE_ACCESS_DENIED'),
])
async def test_manual_check_errors(persian, payment_functions, case, code, key):
    item = record('atlaspay')
    payment_functions['get_payment_record'] = AsyncMock(return_value=None if case == 'missing' else item)
    user = SimpleNamespace(id=999 if case == 'forbidden' else 123, language='fa')
    with pytest.raises(HTTPException) as error:
        await payment_functions['check_payment_status']('invalid' if case == 'invalid' else 'atlaspay', 7, user, object())
    assert error.value.status_code == code
    assert error.value.detail == persian.t(key)


@pytest.mark.asyncio
@pytest.mark.parametrize(('case', 'code', 'key'), [
    ('invalid', 400, 'CABINET_PAYMENT_INVALID_METHOD'),
    ('missing', 404, 'CABINET_BALANCE_PAYMENT_NOT_FOUND'),
    ('forbidden', 403, 'CABINET_BALANCE_ACCESS_DENIED'),
])
async def test_details_errors(persian, payment_functions, case, code, key):
    item = record('atlaspay')
    payment_functions['get_payment_record'] = AsyncMock(return_value=None if case == 'missing' else item)
    user = SimpleNamespace(id=999 if case == 'forbidden' else 123, language='fa')
    with pytest.raises(HTTPException) as error:
        await payment_functions['get_pending_payment_details'](
            'invalid' if case == 'invalid' else 'atlaspay', 7, user, object())
    assert error.value.status_code == code
    assert error.value.detail == persian.t(key)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['atlaspay', 'hooshpay', 'tonpays', 'nowpayments'])
async def test_details_and_paginated_list(persian, payment_functions, provider):
    item = record(provider, 'amount_mismatch')
    foreign = record(provider)
    foreign.user.id = 999
    payment_functions['get_payment_record'] = AsyncMock(return_value=item)
    payment_functions['list_recent_pending_payments'] = AsyncMock(return_value=[foreign, item])
    details = await payment_functions['get_pending_payment_details'](provider, 7, item.user, object())
    assert details.status_text == 'مغایرت مبلغ' and details.amount_kopeks == 250000
    page = await payment_functions['get_pending_payments'](1, 10, item.user, object())
    assert page.total == 1 and page.pages == 1 and len(page.items) == 1
    assert page.items[0].status_text == details.status_text
    payment_functions['list_recent_pending_payments'].return_value = []
    page = await payment_functions['get_pending_payments'](1, 10, item.user, object())
    assert page.items == [] and page.total == 0 and page.pages == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['invalid', 'manual'])
async def test_latest_unsupported_errors(persian, payment_functions, provider):
    db = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await payment_functions['get_latest_payment_by_method'](provider, record('atlaspay').user, db)
    assert error.value.status_code == 400
    assert error.value.detail == persian.t('CABINET_PAYMENT_INVALID_METHOD' if provider == 'invalid'
                                          else 'CABINET_PAYMENT_LATEST_UNSUPPORTED')
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['atlaspay', 'hooshpay', 'tonpays', 'nowpayments'])
@pytest.mark.parametrize('has_recent', [True, False])
async def test_latest_payment_with_real_query(persian, payment_functions, provider, has_recent):
    from app.database.models import AtlasPayPayment, HooshPayPayment, TonPaysPayment, NowPaymentsPayment

    models = dict(atlaspay=AtlasPayPayment, hooshpay=HooshPayPayment,
                  tonpays=TonPaysPayment, nowpayments=NowPaymentsPayment)
    model = models[provider]
    now = datetime.now(UTC)
    owner = record(provider).user
    rows = [dict(id=1, user_id=owner.id, created_at=now - timedelta(hours=2)),
            dict(id=2, user_id=999, created_at=now)]
    if has_recent:
        rows += [dict(id=3, user_id=owner.id, created_at=now - timedelta(minutes=10)),
                 dict(id=4, user_id=owner.id, created_at=now - timedelta(minutes=5)),
                 dict(id=5, user_id=owner.id, created_at=now - timedelta(minutes=5))]
    for row in rows:
        row.update(order_id=f'order-{row["id"]}', amount_kopeks=250000, status='success', is_paid=True)
    engine = create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as connection:
            model.__table__.create(connection)
            connection.execute(model.__table__.insert(), rows)

        async def execute(statement):
            with engine.connect() as connection:
                matches = connection.execute(statement).mappings().all()
            assert len(matches) <= 1
            payment = None
            if matches:
                payment = SimpleNamespace(**dict(matches[0]), user=owner)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: payment))

        db = SimpleNamespace(execute=AsyncMock(side_effect=execute))
        if has_recent:
            result = await payment_functions['get_latest_payment_by_method'](provider, owner, db)
            assert result.id == 5 and result.identifier == 'order-5'
            assert result.amount_kopeks == 250000 and result.amount_rubles == 2500
            assert result.status_text == persian.t('ADMIN_PAYMENT_STATUS_PAID')
            assert result.user_id == owner.id and result.is_paid
        else:
            with pytest.raises(HTTPException) as error:
                await payment_functions['get_latest_payment_by_method'](provider, owner, db)
            assert error.value.status_code == 404
            assert error.value.detail == persian.t('CABINET_PAYMENT_NO_RECENT')
    finally:
        engine.dispose()
