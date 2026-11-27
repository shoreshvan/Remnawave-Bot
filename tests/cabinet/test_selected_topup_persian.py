from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import balance
from app.cabinet.schemas.balance import PaymentMethodResponse, TopUpRequest
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['atlaspay', 'hooshpay', 'nowpayments', 'tonpays'])
async def test_topup_without_language_uses_persian(monkeypatch, persian, provider):
    user = SimpleNamespace(id=1, telegram_id=123, language=None, restriction_topup=False)
    method = PaymentMethodResponse(id=provider, name=provider, min_amount_kopeks=100,
                                   max_amount_kopeks=100000, is_available=True)
    monkeypatch.setattr(balance, 'get_payment_methods', AsyncMock(return_value=[method]))
    if provider == 'tonpays':
        with pytest.raises(HTTPException) as error:
            await balance.create_topup(TopUpRequest(amount_kopeks=12345, payment_method=provider), user, AsyncMock())
        assert error.value.detail == 'این روش پرداخت فقط از طریق ربات تلگرام در دسترس است.'
        return
    monkeypatch.setattr(type(balance.settings), f'is_{provider}_enabled', lambda self: True)
    create = AsyncMock(return_value={'payment_url': 'https://example.test', 'local_payment_id': 7})
    monkeypatch.setattr(balance, 'PaymentService', lambda: SimpleNamespace(**{f'create_{provider}_payment': create}))
    response = await balance.create_topup(
        TopUpRequest(amount_kopeks=12345, payment_method=provider), user, AsyncMock()
    )
    assert response.amount_kopeks == 12345
    assert create.await_args.kwargs['language'] == 'fa'
    assert 'شارژ موجودی' in create.await_args.kwargs['description']
