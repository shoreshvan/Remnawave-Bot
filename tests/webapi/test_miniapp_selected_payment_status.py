from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import PaymentMethod
from app.services import payment_verification_service
from app.webapi.routes import miniapp
from app.webapi.schemas.miniapp import MiniAppPaymentStatusQuery
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['atlaspay', 'hooshpay', 'tonpays', 'nowpayments'])
@pytest.mark.parametrize(('paid', 'raw_status', 'expected'), [
    (False, 'processing', 'pending'),
    (False, 'success', 'pending'),
    (False, 'failed', 'failed'),
    (True, 'success', 'paid'),
])
async def test_selected_gateway_status_requires_ownership_and_paid_flag(monkeypatch, persian, method, paid, raw_status, expected):
    payment = SimpleNamespace(currency='IRT', paid_at=None, transaction_id=4)
    record = SimpleNamespace(user=SimpleNamespace(id=1), is_paid=paid, status=raw_status,
                             amount_kopeks=12345, payment=payment, identifier='order-1', local_id=7)
    lookup = AsyncMock(return_value=record)
    monkeypatch.setattr(payment_verification_service, 'get_payment_record', lookup)
    query = MiniAppPaymentStatusQuery(method=method, localPaymentId=7)
    result = await miniapp._resolve_payment_status_entry(
        payment_service=object(), db=object(), user=SimpleNamespace(id=1, language=None), query=query,
    )
    assert result.status == expected and result.is_paid is paid
    assert result.amount_kopeks == 12345 and result.external_id == 'order-1'
    assert lookup.await_args.args[1:] == (PaymentMethod(method), 7)
    record.user.id = 2
    other = await miniapp._resolve_payment_status_entry(
        payment_service=object(), db=object(), user=SimpleNamespace(id=1, language=None), query=query,
    )
    assert other.message == 'پرداخت یافت نشد' and other.amount_kopeks is None
