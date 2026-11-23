"""Регистрация TonPays в общих точках бота.

Одношлюзовый провайдер без суб-методов (одна кнопка «картка-в-картку»):
проверяется именно подключение, чтобы разъехавшаяся регистрация падала
тестом, а не в проде.
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
    monkeypatch.setattr(settings, 'TONPAYS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'TONPAYS_API_KEY', 'tg_key', raising=False)


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'TONPAYS_ENABLED', False, raising=False)


# ---------------------------------------------------------------------------
# Реестры, от которых зависят выручка, кабинет, настройки и статистика
# ---------------------------------------------------------------------------


def test_present_in_registries() -> None:
    assert 'tonpays' in {m.value for m in PaymentMethod}
    # Иначе шлюз молча выпадет из выручки, партнёрки и отчётов
    assert 'tonpays' in REAL_PAYMENT_METHODS
    # Иначе строка конфига не заведётся и метод не покажется в кабинете/настройках
    assert 'tonpays' in DEFAULT_METHOD_ORDER
    assert 'tonpays' in _get_method_defaults()


def test_single_method_has_no_sub_options() -> None:
    # TonPays — одна кнопка «карта-в-карту», без выбора СБП/карты.
    assert _get_method_defaults()['tonpays']['available_sub_options'] is None


# ---------------------------------------------------------------------------
# Видимость в списке способов пополнения
# ---------------------------------------------------------------------------


def test_hidden_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    ids = {m['id'] for m in get_available_payment_methods()}
    assert 'tonpays' not in ids


def test_visible_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    found = [m for m in get_available_payment_methods() if m['id'] == 'tonpays']
    assert len(found) == 1
    assert found[0]['callback'] == 'topup_tonpays'
    assert found[0]['name']


def test_availability_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    assert is_payment_method_available('tonpays') is True
    _disable(monkeypatch)
    assert is_payment_method_available('tonpays') is False


def test_guest_method_split() -> None:
    # Без суб-методов — всегда (база, None).
    assert _split_guest_payment_method('tonpays') == ('tonpays', None)


# ---------------------------------------------------------------------------
# Фоновая сверка зависших платежей
# ---------------------------------------------------------------------------


def test_pending_predicate() -> None:
    from app.services import payment_verification_service as pvs

    class _P:
        is_paid = False
        status = 'pending'

    assert pvs._is_tonpays_pending(_P()) is True
    # processing / need_action ещё могут стать оплаченными
    _P.status = 'processing'
    assert pvs._is_tonpays_pending(_P()) is True
    _P.status = 'need_action'
    assert pvs._is_tonpays_pending(_P()) is True

    _P.status = 'success'
    assert pvs._is_tonpays_pending(_P()) is False
    _P.status = 'rejected'
    assert pvs._is_tonpays_pending(_P()) is False
    _P.status = 'pending'
    _P.is_paid = True
    assert pvs._is_tonpays_pending(_P()) is False


def test_enabled_in_verification_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import payment_verification_service as pvs

    assert PaymentMethod.TONPAYS in pvs.SUPPORTED_MANUAL_CHECK_METHODS
    assert PaymentMethod.TONPAYS in pvs.SUPPORTED_AUTO_CHECK_METHODS

    _enable(monkeypatch)
    assert PaymentMethod.TONPAYS in pvs.get_enabled_auto_methods()

    _disable(monkeypatch)
    assert PaymentMethod.TONPAYS not in pvs.get_enabled_auto_methods()


# ---------------------------------------------------------------------------
# Панель настроек: TONPAYS_* должны попасть в свою категорию
# ---------------------------------------------------------------------------


def test_settings_category_registered() -> None:
    assert 'TONPAYS' in BotConfigurationService.CATEGORY_TITLES
    assert BotConfigurationService._resolve_category_key('TONPAYS_ENABLED') == 'TONPAYS'
    assert BotConfigurationService._resolve_category_key('TONPAYS_API_KEY') == 'TONPAYS'


# ---------------------------------------------------------------------------
# Статус-маппинг провайдера
# ---------------------------------------------------------------------------


def test_status_map_covers_provider_statuses() -> None:
    from app.services.payment.tonpays import TONPAYS_FINAL_STATUSES, TONPAYS_PENDING_STATUSES, TONPAYS_STATUS_MAP

    # Все статусы из документации должны быть известны.
    for provider_status in (
        'pending',
        'processing',
        'completed',
        'need_action',
        'rejected',
        'expired',
        'canceled',
    ):
        assert provider_status in TONPAYS_STATUS_MAP

    assert 'pending' in TONPAYS_PENDING_STATUSES
    assert 'processing' in TONPAYS_PENDING_STATUSES
    assert 'need_action' in TONPAYS_PENDING_STATUSES
    assert 'amount_mismatch' in TONPAYS_FINAL_STATUSES
