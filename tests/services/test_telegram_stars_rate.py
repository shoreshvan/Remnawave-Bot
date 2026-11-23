"""Regression tests for the Telegram Stars ↔ home-currency conversion rate.

Home currency is Toman. `TELEGRAM_STARS_RATE_RUB` is Toman-per-⭐ (the
`_RUB` suffix is kept only for env / system-setting back-compat; the
value is home-currency units, not rubles). `rubles_to_stars` /
`stars_to_rubles` are currency-agnostic — they divide/multiply by the
rate — so the historical rounding bug they guard against still matters.

The bug from 2026-05-16: with a rate of 1.3, asking the bot to top up
150 units produced a quote of 115 ⭐ (round(150/1.3)=115), and the
return-conversion credited 115 × 1.3 = 149.50 — a built-in rounding
loss on every transaction.

These tests pin:
  1. The default rate is 4000.0 (Toman per ⭐ — an operator pricing
     decision, tunable via env / admin panel).
  2. An amount that is a whole multiple of the rate round-trips
     losslessly. The parametrized case runs at rate=1.0 to exercise
     that integer-multiple invariant directly.
"""

from __future__ import annotations

import pytest

from app.config import Settings, settings


def test_default_stars_rate_is_toman_per_star() -> None:
    """REGRESSION: default rate is the operator's Toman-per-⭐ price.

    Home currency is Toman; the default is 4000 Toman per star (tunable
    via env / admin panel). Pinned so an accidental edit back to a
    ruble-era value (1.0, 1.3) can't silently ship and mis-price every
    Stars top-up.
    """
    default_rate = Settings.model_fields['TELEGRAM_STARS_RATE_RUB'].default
    assert default_rate == 4000.0, (
        f'Default TELEGRAM_STARS_RATE_RUB must be 4000.0 (Toman per ⭐). '
        f'Got {default_rate!r}.'
    )


@pytest.mark.parametrize('rubles', [50, 100, 150, 200, 500, 1000, 5000])
def test_integer_ruble_amounts_round_trip_losslessly(
    rubles: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION: at rate=1.0, integer ruble top-ups credit back exactly.

    Pre-fix at rate=1.3:
      150 ₽ → rubles_to_stars(150) = round(115.38) = 115 ⭐
      stars_to_rubles(115) = 115 × 1.3 = 149.50 ₽ (loss = 0.50 ₽)

    Post-fix at rate=1.0 this loss is gone for any integer ruble input.
    """
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 1.0, raising=False)

    stars = settings.rubles_to_stars(float(rubles))
    rubles_back = settings.stars_to_rubles(stars)

    assert stars == rubles, f'{rubles} ₽ must quote {rubles} ⭐ at rate=1.0, got {stars}'
    assert rubles_back == float(rubles), (
        f'{rubles} ₽ → {stars} ⭐ → {rubles_back} ₽ is not lossless (delta {rubles_back - rubles:+.2f} ₽)'
    )


def test_rubles_to_stars_rejects_invalid_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive check: zero/negative rate must raise rather than divide-by-zero."""
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 0, raising=False)
    with pytest.raises(ValueError):
        settings.rubles_to_stars(100)

    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', -1, raising=False)
    with pytest.raises(ValueError):
        settings.rubles_to_stars(100)


def test_rubles_to_stars_clamps_to_minimum_one_star(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even at rate=1.0, a 0 ₽ request must return ≥1 ⭐ (Telegram requires positive amount)."""
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 1.0, raising=False)
    assert settings.rubles_to_stars(0) == 1
    # Negative inputs are caller-error but should not return <1.
    assert settings.rubles_to_stars(-50) == 1


def test_rate_change_is_propagated_through_telegram_stars_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TelegramStarsService.calculate_*` helpers must defer to settings — no hardcoded copies.

    Pinned because both `external/telegram_stars.py` and
    `services/payment/stars.py` historically had drift risk: if one
    hardcoded a rate and the other used settings, the invoice quote
    and the post-payment credit would diverge silently.
    """
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 2.5, raising=False)

    from app.external.telegram_stars import TelegramStarsService

    assert TelegramStarsService.calculate_stars_from_rubles(100.0) == settings.rubles_to_stars(100.0)
    rubles_back = TelegramStarsService.calculate_rubles_from_stars(40)
    assert float(rubles_back) == settings.stars_to_rubles(40)
