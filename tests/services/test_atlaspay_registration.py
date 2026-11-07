"""Регистрация AtlasPay в общих точках бота.

Одношлюзовый провайдер без суб-методов (одна кнопка «карта-в-карту» через
Telegram mini-app): проверяется именно подключение, чтобы разъехавшаяся
регистрация падала тестом, а не в проде.
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
    monkeypatch.setattr(settings, 'ATLASPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ATLASPAY_API_KEY', 'ap_key', raising=False)


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'ATLASPAY_ENABLED', False, raising=False)


# ---------------------------------------------------------------------------
# Реестры, от которых зависят выручка, кабинет, настройки и статистика
# ---------------------------------------------------------------------------


def test_present_in_registries() -> None:
    assert 'atlaspay' in {m.value for m in PaymentMethod}
    # Иначе шлюз молча выпадет из выручки, партнёрки и отчётов
    assert 'atlaspay' in REAL_PAYMENT_METHODS
    # Иначе строка конфига не заведётся и метод не покажется в кабинете/настройках
    assert 'atlaspay' in DEFAULT_METHOD_ORDER
    assert 'atlaspay' in _get_method_defaults()


def test_single_method_has_no_sub_options() -> None:
    # AtlasPay — одна кнопка, без выбора СБП/карты.
    assert _get_method_defaults()['atlaspay']['available_sub_options'] is None


def test_method_limits() -> None:
    # Кф 50000 / скф 2500000 تومان по требованию.
    defaults = _get_method_defaults()['atlaspay']
    assert defaults['default_min'] == 5000000
    assert defaults['default_max'] == 250000000


# ---------------------------------------------------------------------------
# Видимость в списке способов пополнения
# ---------------------------------------------------------------------------


def test_hidden_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    ids = {m['id'] for m in get_available_payment_methods()}
    assert 'atlaspay' not in ids


def test_visible_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    found = [m for m in get_available_payment_methods() if m['id'] == 'atlaspay']
    assert len(found) == 1
    assert found[0]['callback'] == 'topup_atlaspay'
    assert found[0]['name']


def test_availability_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    assert is_payment_method_available('atlaspay') is True
    _disable(monkeypatch)
    assert is_payment_method_available('atlaspay') is False


def test_guest_method_split() -> None:
    # Без суб-методов — всегда (база, None).
    assert _split_guest_payment_method('atlaspay') == ('atlaspay', None)


# ---------------------------------------------------------------------------
# Фоновая сверка зависших платежей
# ---------------------------------------------------------------------------


def test_pending_predicate() -> None:
    from app.services import payment_verification_service as pvs

    class _P:
        is_paid = False
        status = 'pending'

    assert pvs._is_atlaspay_pending(_P()) is True
    # admin_review / underpaid_* ещё могут стать оплаченными
    _P.status = 'processing'
    assert pvs._is_atlaspay_pending(_P()) is True
    _P.status = 'underpaid_review'
    assert pvs._is_atlaspay_pending(_P()) is True

    _P.status = 'success'
    assert pvs._is_atlaspay_pending(_P()) is False
    _P.status = 'manual_review'
    assert pvs._is_atlaspay_pending(_P()) is False
    _P.status = 'rejected'
    assert pvs._is_atlaspay_pending(_P()) is False
    _P.status = 'pending'
    _P.is_paid = True
    assert pvs._is_atlaspay_pending(_P()) is False


def test_enabled_in_verification_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import payment_verification_service as pvs

    assert PaymentMethod.ATLASPAY in pvs.SUPPORTED_MANUAL_CHECK_METHODS
    assert PaymentMethod.ATLASPAY in pvs.SUPPORTED_AUTO_CHECK_METHODS

    _enable(monkeypatch)
    assert PaymentMethod.ATLASPAY in pvs.get_enabled_auto_methods()

    _disable(monkeypatch)
    assert PaymentMethod.ATLASPAY not in pvs.get_enabled_auto_methods()


# ---------------------------------------------------------------------------
# Панель настроек: ATLASPAY_* должны попасть в свою категорию
# ---------------------------------------------------------------------------


def test_settings_category_registered() -> None:
    assert 'ATLASPAY' in BotConfigurationService.CATEGORY_TITLES
    assert BotConfigurationService._resolve_category_key('ATLASPAY_ENABLED') == 'ATLASPAY'
    assert BotConfigurationService._resolve_category_key('ATLASPAY_API_KEY') == 'ATLASPAY'


# ---------------------------------------------------------------------------
# Статус-маппинг провайдера
# ---------------------------------------------------------------------------


def test_status_map_covers_provider_statuses() -> None:
    from app.services.payment.atlaspay import (
        ATLASPAY_FINAL_STATUSES,
        ATLASPAY_PENDING_STATUSES,
        ATLASPAY_STATUS_MAP,
        ATLASPAY_SUCCESS_STATUSES,
    )

    # Все статусы из документации должны быть известны.
    for provider_status in (
        'awaiting_payment',
        'admin_review',
        'underpaid_review',
        'underpaid_awaiting_remainder',
        'confirmed',
        'settled',
        'rejected',
        'expired',
        'cancelled',
    ):
        assert provider_status in ATLASPAY_STATUS_MAP

    assert 'pending' in ATLASPAY_PENDING_STATUSES
    assert 'processing' in ATLASPAY_PENDING_STATUSES
    assert 'underpaid_review' in ATLASPAY_PENDING_STATUSES
    assert 'confirmed' in ATLASPAY_SUCCESS_STATUSES
    assert 'settled' in ATLASPAY_SUCCESS_STATUSES
    assert 'amount_mismatch' in ATLASPAY_FINAL_STATUSES
