"""Persian quick amounts and retry limits preserve monetary scale and callbacks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.handlers.balance import main
from app.keyboards import topup_amounts
from app.utils.topup_retry import format_topup_amount_retry
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.parametrize('currency', ['تومان', 'واحد آزمایشی'])
@pytest.mark.parametrize(('minimum', 'maximum', 'too_low'), [(5055, 10000, True), (5055, 10000, False), (10000, 5000, True)])
def test_retry_limits_have_correct_currency(persian, monkeypatch, currency, minimum, maximum, too_low):
    monkeypatch.setattr(settings, 'CURRENCY_SYMBOL', currency)
    text = format_topup_amount_retry('fa', minimum, maximum, too_low=too_low)
    assert currency in text
    assert 'روبل' not in text and '₽' not in text and '{' not in text
    if minimum == 5055:
        assert f'50.55 {currency}' in text and f'100 {currency}' in text
        assert '51' in text
    else:
        assert 'مبلغ مجازی' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['atlaspay', 'hooshpay', 'tonpays', 'nowpayments'])
async def test_quick_amount_button_routes_same_amount(persian, monkeypatch, provider):
    monkeypatch.setattr(topup_amounts, '_load_quick_amounts', AsyncMock(return_value=[250000]))
    monkeypatch.setattr(settings, 'CURRENCY_SYMBOL', 'تومان')
    markup = await topup_amounts.get_topup_amount_keyboard(provider, 'fa', db=AsyncMock())
    button = markup.inline_keyboard[0][0]
    assert button.text == settings.format_price(250000, round_kopeks=False)
    assert button.callback_data == f'topup_amount|{provider}|250000'
    route = AsyncMock(return_value=True)
    monkeypatch.setattr(main, 'route_payment_by_method', route)
    callback = SimpleNamespace(data=button.callback_data, answer=AsyncMock(), message=SimpleNamespace())
    user, state = SimpleNamespace(language='fa'), AsyncMock()
    await main.handle_topup_amount_callback(callback, user, state)
    route.assert_awaited_once_with(callback.message, user, 250000, state, provider)


@pytest.mark.asyncio
@pytest.mark.parametrize(('data', 'key'), [('invalid', 'DEVICES_INVALID_REQUEST'), ('topup_amount|atlaspay|0', 'PAYMENT_UI_INVALID_AMOUNT')])
async def test_invalid_buttons_show_persian_alert(persian, data, key):
    callback = SimpleNamespace(data=data, answer=AsyncMock())
    await main.handle_topup_amount_callback(callback, SimpleNamespace(language='fa'), AsyncMock())
    callback.answer.assert_awaited_once_with(persian.t(key), show_alert=True)
