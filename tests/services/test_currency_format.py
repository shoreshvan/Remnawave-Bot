"""Tests for Toman currency formatting and home-currency defaults (Phase 2).

Домашняя валюта сервиса — иранский туман: внутренние суммы хранятся в
минорных единицах (1/100 тумана), отображение — с символом «تومان» и
разделителями тысяч. Провайдерские таблицы платежей (Pal24, CloudPayments
и т.п.) хранят валюту провайдера и остаются в RUB.
"""

from __future__ import annotations

from app.config import settings
from app.database.models import CloudPaymentsPayment, GuestPurchase, Pal24Payment


class TestFormatPriceToman:
    def test_symbol_is_toman(self) -> None:
        assert settings.format_price(15000, round_kopeks=False) == '150 تومان'

    def test_thousands_separator(self) -> None:
        assert settings.format_price(150000, round_kopeks=False) == '1,500 تومان'
        assert settings.format_price(123456789, round_kopeks=False) == '1,234,567.89 تومان'

    def test_zero(self) -> None:
        assert settings.format_price(0, round_kopeks=False) == '0 تومان'

    def test_negative(self) -> None:
        assert settings.format_price(-25000, round_kopeks=False) == '-250 تومان'

    def test_trailing_zero_minor_trimmed(self) -> None:
        assert settings.format_price(15050, round_kopeks=False) == '150.5 تومان'
        assert settings.format_price(15055, round_kopeks=False) == '150.55 تومان'

    def test_rounding_down_at_half(self) -> None:
        # ≤0.50 — вниз
        assert settings.format_price(15050, round_kopeks=True) == '150 تومان'

    def test_rounding_up_above_half(self) -> None:
        # >0.50 — вверх
        assert settings.format_price(15051, round_kopeks=True) == '151 تومان'

    def test_rounding_keeps_thousands_separator(self) -> None:
        assert settings.format_price(123456789, round_kopeks=True) == '1,234,568 تومان'

    def test_bigint_scale_amounts(self) -> None:
        # Суммы выше старого потолка int32 (21 474 836.47) — после миграции BigInteger
        assert settings.format_price(100_000_000_000, round_kopeks=False) == '1,000,000,000 تومان'

    def test_custom_separator_setting(self) -> None:
        separator = settings.PRICE_THOUSANDS_SEPARATOR
        assert settings.format_price(150000, round_kopeks=False).startswith(f'1{separator}500')


class TestHomeCurrencyDefaults:
    def test_home_currency_code_setting(self) -> None:
        assert settings.HOME_CURRENCY_CODE == 'IRT'

    def test_currency_symbol_setting(self) -> None:
        assert settings.CURRENCY_SYMBOL == 'تومان'

    def test_guest_purchase_default_currency_is_home_currency(self) -> None:
        default = GuestPurchase.__table__.c.currency.default
        assert default is not None
        assert default.arg == settings.HOME_CURRENCY_CODE

    def test_provider_table_defaults_stay_native(self) -> None:
        # Провайдерские платёжные таблицы хранят валюту провайдера (RUB)
        for model in (Pal24Payment, CloudPaymentsPayment):
            default = model.__table__.c.currency.default
            assert default is not None, model.__name__
            assert default.arg == 'RUB', model.__name__


class TestTextsDelegatesToSettings:
    def test_texts_format_price_matches_settings(self) -> None:
        from app.localization.texts import get_texts

        texts = get_texts('fa')
        assert texts.format_price(150000) == settings.format_price(150000)

    def test_format_price_kopeks_delegates(self) -> None:
        from app.utils.formatting import format_price_kopeks

        assert format_price_kopeks(150000) == settings.format_price(150000, round_kopeks=False)
        assert format_price_kopeks(150051, compact=True) == settings.format_price(150051, round_kopeks=True)

    def test_format_quick_amount_delegates(self) -> None:
        from app.keyboards.topup_amounts import format_quick_amount

        assert format_quick_amount(150000) == '1,500 تومان'
        assert format_quick_amount(150050) == '1,500.5 تومان'
