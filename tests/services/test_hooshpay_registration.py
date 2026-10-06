"""Регистрация HooshPay в общих точках бота.

Клиент HooshPay можно написать безупречно и не подключить: кнопка не появится,
способ не попадёт в кабинет и настройки, а фоновая сверка не увидит зависшие
платежи — ровно это и произошло в первой итерации («мёртвый код»). Здесь
проверяется именно подключение одношлюзового (без СБП/карты суб-методов)
провайдера, чтобы разъехавшаяся регистрация падала тестом, а не в проде.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import PaymentMethod
from app.services.payment_method_config_service import DEFAULT_METHOD_ORDER, _get_method_defaults
from app.services.payment_service import _split_guest_payment_method
from app.services.system_settings_service import BotConfigurationService
from app.utils.payment_utils import get_available_payment_methods, is_payment_method_available


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    # is_hooshpay_enabled() requires flag + API key; the webhook secret is only
    # needed for is_hooshpay_configured() (webhook mounting).
    monkeypatch.setattr(settings, 'HOOSHPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'HOOSHPAY_API_KEY', 'hp_key', raising=False)
    monkeypatch.setattr(settings, 'HOOSHPAY_API_SECRET', 'hp_secret', raising=False)


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'HOOSHPAY_ENABLED', False, raising=False)


# ---------------------------------------------------------------------------
# Реестры, от которых зависят выручка, кабинет, настройки и статистика
# ---------------------------------------------------------------------------


def test_present_in_registries() -> None:
    assert 'hooshpay' in {m.value for m in PaymentMethod}
    # Иначе шлюз молча выпадет из выручки, партнёрки и отчётов
    assert 'hooshpay' in REAL_PAYMENT_METHODS
    # Иначе строка конфига не заведётся и метод не покажется в кабинете/настройках
    assert 'hooshpay' in DEFAULT_METHOD_ORDER
    assert 'hooshpay' in _get_method_defaults()


def test_single_method_has_no_sub_options() -> None:
    # HooshPay — одна кнопка «карта-в-карту», без выбора СБП/карты.
    assert _get_method_defaults()['hooshpay']['available_sub_options'] is None


# ---------------------------------------------------------------------------
# Видимость в списке способов пополнения
# ---------------------------------------------------------------------------


def test_hidden_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    ids = {m['id'] for m in get_available_payment_methods()}
    assert 'hooshpay' not in ids


def test_visible_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    found = [m for m in get_available_payment_methods() if m['id'] == 'hooshpay']
    assert len(found) == 1
    assert found[0]['callback'] == 'topup_hooshpay'
    assert found[0]['name']


def test_availability_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    assert is_payment_method_available('hooshpay') is True
    _disable(monkeypatch)
    assert is_payment_method_available('hooshpay') is False


def test_guest_method_split() -> None:
    # Без суб-методов — всегда (база, None).
    assert _split_guest_payment_method('hooshpay') == ('hooshpay', None)


# ---------------------------------------------------------------------------
# Фоновая сверка зависших платежей
# ---------------------------------------------------------------------------


def test_pending_predicate() -> None:
    from app.services import payment_verification_service as pvs

    class _P:
        is_paid = False
        status = 'pending'

    assert pvs._is_hooshpay_pending(_P()) is True

    _P.status = 'success'
    assert pvs._is_hooshpay_pending(_P()) is False
    _P.status = 'expired'
    assert pvs._is_hooshpay_pending(_P()) is False
    _P.status = 'pending'
    _P.is_paid = True
    assert pvs._is_hooshpay_pending(_P()) is False


def test_enabled_in_verification_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import payment_verification_service as pvs

    assert PaymentMethod.HOOSHPAY in pvs.SUPPORTED_MANUAL_CHECK_METHODS
    assert PaymentMethod.HOOSHPAY in pvs.SUPPORTED_AUTO_CHECK_METHODS

    _enable(monkeypatch)
    assert PaymentMethod.HOOSHPAY in pvs.get_enabled_auto_methods()

    _disable(monkeypatch)
    assert PaymentMethod.HOOSHPAY not in pvs.get_enabled_auto_methods()


# ---------------------------------------------------------------------------
# Панель настроек: HOOSHPAY_* должны попасть в свою категорию
# ---------------------------------------------------------------------------


def test_settings_category_registered() -> None:
    assert 'HOOSHPAY' in BotConfigurationService.CATEGORY_TITLES
    assert BotConfigurationService._resolve_category_key('HOOSHPAY_ENABLED') == 'HOOSHPAY'
    assert BotConfigurationService._resolve_category_key('HOOSHPAY_API_KEY') == 'HOOSHPAY'


# ---------------------------------------------------------------------------
# Включение без секрета вебхука (webhook-less / polling-only)
# ---------------------------------------------------------------------------


def test_enabled_without_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Секрет нужен ТОЛЬКО вебхуку. Без него шлюз обязан работать (REST — по
    X-API-KEY, подтверждение — периодическим опросом), иначе кнопка не покажется
    тем, кто не настраивает вебхук — ровно этот баг и был."""
    monkeypatch.setattr(settings, 'HOOSHPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'HOOSHPAY_API_KEY', 'hp_key', raising=False)
    monkeypatch.setattr(settings, 'HOOSHPAY_API_SECRET', None, raising=False)

    assert settings.is_hooshpay_enabled() is True
    assert is_payment_method_available('hooshpay') is True
    assert 'hooshpay' in {m['id'] for m in get_available_payment_methods()}
    # Но вебхук без секрета принимать нельзя — он не монтируется.
    assert settings.is_hooshpay_configured() is False

    # Без API-ключа шлюз всё равно выключен (одного флага мало).
    monkeypatch.setattr(settings, 'HOOSHPAY_API_KEY', None, raising=False)
    assert settings.is_hooshpay_enabled() is False
