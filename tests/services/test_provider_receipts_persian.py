"""Persian descriptions at invoice creation and atomic credit boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import AnorePayment
from app.services.payment import anore, cispay
from app.services.payment_service import PaymentService
from tests.fixtures.persian import persian  # noqa: F401
from tests.services.test_payment_service_cispay import Session, creation, payment, payload, service  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('description', [None, 'شرح اختصاصی', ''])
async def test_cispay_invoice_default_and_custom_description(persian, service, creation, description):
    db, remote = creation
    db.user.language = 'fa'
    result = await service.create_cispay_payment(
        db, user_id=77, amount_kopeks=50000, description=description, language='ru',
    )
    assert result is not None
    expected = 'شارژ موجودی' if description is None else description
    assert remote.create_payment.await_args.kwargs['description'] == (expected or None)
    assert db.payment.description == expected
    assert db.payment.amount_kopeks == 50000


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['anore', 'cispay'])
async def test_paid_callback_receipt_and_replay(persian, monkeypatch, provider):
    module = anore if provider == 'anore' else cispay
    instance = PaymentService.__new__(PaymentService)
    instance.bot = None
    monkeypatch.setattr(instance, f'_after_{provider}_payment', AsyncMock())
    monkeypatch.setattr(type(module.settings), f'get_{provider}_display_name', lambda self: 'درگاه ویژه')
    transaction = AsyncMock(return_value=SimpleNamespace(id=9))
    monkeypatch.setattr(module, 'create_transaction', transaction)
    if provider == 'anore':
        record = AnorePayment(
            id=7, user_id=77, order_id='anore_test', anore_payment_id=None,
            amount_kopeks=50000, currency='RUB', status='new', is_paid=False,
            is_test=False, metadata_json={}, processed_events=[],
        )
        data = {'id': 'invoice-test', 'orderId': 'anore_test', 'amount': 500,
                'currency': 'RUB', 'status': 'paid', 'paid': True}
    else:
        record, data = payment(), payload()
    db = Session(record)
    db.user.language = 'fa'
    accept = getattr(instance, f'_accept_{provider}_state')
    validation = {'require_order': True} if provider == 'anore' else {'require_store': True}
    assert await accept(db, data, **validation)
    assert transaction.await_args.kwargs['description'] == 'شارژ از طریق درگاه ویژه'
    assert transaction.await_args.kwargs['amount_kopeks'] == 50000
    assert db.user.balance_kopeks == 50100
    assert await accept(db, data, **validation)
    transaction.assert_awaited_once()
    assert db.user.balance_kopeks == 50100


@pytest.mark.asyncio
@pytest.mark.parametrize('guest', [False, True])
async def test_anore_default_invoice_uses_account_or_guest_language(persian, monkeypatch, guest):
    db = Session()
    db.user.language = 'fa'
    instance = PaymentService.__new__(PaymentService)
    config = SimpleNamespace(methods=['card'], min_amount_kopeks=100, max_amount_kopeks=100000, is_test=False)
    monkeypatch.setattr(type(anore.settings), 'is_anore_enabled', lambda self: True)
    monkeypatch.setattr('app.services.payment_method_config_service.get_config_by_method_id', AsyncMock(return_value=None))

    async def record(_db, **values):
        db.payment = AnorePayment(id=7, **values)
        return db.payment

    async def remote(**values):
        return {'id': 'invoice-test', 'orderId': values['order_id'], 'amount': 500,
                'currency': 'RUB', 'status': 'new', 'paymentUrl': 'https://pay.example/invoice'}

    create = AsyncMock(side_effect=remote)
    monkeypatch.setattr(anore, 'create_record', record)
    monkeypatch.setattr(anore, 'anore_service', SimpleNamespace(snapshot=lambda: config, create_payment=create))
    result = await instance.create_anore_payment(
        db, user_id=None if guest else 77, amount_kopeks=50000, language='fa' if guest else 'ru',
    )
    assert result is not None
    assert create.await_args.kwargs['description'] == 'شارژ موجودی'
    assert db.payment.description == 'شارژ موجودی'
    assert result['amount_kopeks'] == 50000
    assert result['currency'] == 'RUB'
