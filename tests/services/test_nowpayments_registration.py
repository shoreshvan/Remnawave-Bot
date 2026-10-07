"""Регистрация NOWPayments в общих точках бота.

Одношлюзовый провайдер без суб-методов (одна кнопка «крипта» с hosted
страницей): проверяется именно подключение, чтобы разъехавшаяся регистрация
падала тестом, а не в проде.
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
    monkeypatch.setattr(settings, 'NOWPAYMENTS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'NOWPAYMENTS_API_KEY', 'np_key', raising=False)


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'NOWPAYMENTS_ENABLED', False, raising=False)


# ---------------------------------------------------------------------------
# Реестры, от которых зависят выручка, кабинет, настройки и статистика
# ---------------------------------------------------------------------------


def test_present_in_registries() -> None:
    assert 'nowpayments' in {m.value for m in PaymentMethod}
    # Иначе шлюз молча выпадет из выручки, партнёрки и отчётов
    assert 'nowpayments' in REAL_PAYMENT_METHODS
    # Иначе строка конфига не заведётся и метод не покажется в кабинете/настройках
    assert 'nowpayments' in DEFAULT_METHOD_ORDER
    assert 'nowpayments' in _get_method_defaults()


def test_single_method_has_no_sub_options() -> None:
    # NOWPayments — одна кнопка, валюту покупатель выбирает на hosted странице.
    assert _get_method_defaults()['nowpayments']['available_sub_options'] is None


def test_method_limits() -> None:
    # Кф 50000 / скф 2500000 تومان по требованию.
    defaults = _get_method_defaults()['nowpayments']
    assert defaults['default_min'] == 5000000
    assert defaults['default_max'] == 250000000


# ---------------------------------------------------------------------------
# Видимость в списке способов пополнения
# ---------------------------------------------------------------------------


def test_hidden_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    ids = {m['id'] for m in get_available_payment_methods()}
    assert 'nowpayments' not in ids


def test_visible_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    found = [m for m in get_available_payment_methods() if m['id'] == 'nowpayments']
    assert len(found) == 1
    assert found[0]['callback'] == 'topup_nowpayments'
    assert found[0]['name']


def test_availability_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    assert is_payment_method_available('nowpayments') is True
    _disable(monkeypatch)
    assert is_payment_method_available('nowpayments') is False


def test_guest_method_split() -> None:
    # Без суб-методов — всегда (база, None).
    assert _split_guest_payment_method('nowpayments') == ('nowpayments', None)


# ---------------------------------------------------------------------------
# Фоновая сверка зависших платежей
# ---------------------------------------------------------------------------


def test_pending_predicate() -> None:
    from app.services import payment_verification_service as pvs

    class _P:
        is_paid = False
        status = 'pending'

    assert pvs._is_nowpayments_pending(_P()) is True
    # confirming/confirmed/sending ещё могут стать finished
    _P.status = 'processing'
    assert pvs._is_nowpayments_pending(_P()) is True

    _P.status = 'success'
    assert pvs._is_nowpayments_pending(_P()) is False
    _P.status = 'manual_review'
    assert pvs._is_nowpayments_pending(_P()) is False
    _P.status = 'expired'
    assert pvs._is_nowpayments_pending(_P()) is False
    _P.status = 'pending'
    _P.is_paid = True
    assert pvs._is_nowpayments_pending(_P()) is False


def test_enabled_in_verification_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import payment_verification_service as pvs

    assert PaymentMethod.NOWPAYMENTS in pvs.SUPPORTED_MANUAL_CHECK_METHODS
    assert PaymentMethod.NOWPAYMENTS in pvs.SUPPORTED_AUTO_CHECK_METHODS

    _enable(monkeypatch)
    assert PaymentMethod.NOWPAYMENTS in pvs.get_enabled_auto_methods()

    _disable(monkeypatch)
    assert PaymentMethod.NOWPAYMENTS not in pvs.get_enabled_auto_methods()


# ---------------------------------------------------------------------------
# Панель настроек: NOWPAYMENTS_* должны попасть в свою категорию
# ---------------------------------------------------------------------------


def test_settings_category_registered() -> None:
    assert 'NOWPAYMENTS' in BotConfigurationService.CATEGORY_TITLES
    assert BotConfigurationService._resolve_category_key('NOWPAYMENTS_ENABLED') == 'NOWPAYMENTS'
    assert BotConfigurationService._resolve_category_key('NOWPAYMENTS_API_KEY') == 'NOWPAYMENTS'


# ---------------------------------------------------------------------------
# Статус-маппинг провайдера: кредитует ТОЛЬКО finished
# ---------------------------------------------------------------------------


def test_status_map_covers_provider_statuses() -> None:
    from app.services.payment.nowpayments import (
        NOWPAYMENTS_FINAL_STATUSES,
        NOWPAYMENTS_PENDING_STATUSES,
        NOWPAYMENTS_STATUS_MAP,
    )

    # Все статусы из документации должны быть известны.
    for provider_status in (
        'waiting',
        'confirming',
        'confirmed',
        'sending',
        'finished',
        'partially_paid',
        'failed',
        'refunded',
        'expired',
    ):
        assert provider_status in NOWPAYMENTS_STATUS_MAP

    # confirmed — это ещё НЕ деньги (только finished кредитует).
    assert NOWPAYMENTS_STATUS_MAP['confirmed'] == ('processing', False)
    assert NOWPAYMENTS_STATUS_MAP['finished'] == ('success', True)
    assert NOWPAYMENTS_STATUS_MAP['partially_paid'] == ('manual_review', False)

    assert 'pending' in NOWPAYMENTS_PENDING_STATUSES
    assert 'processing' in NOWPAYMENTS_PENDING_STATUSES
    assert 'amount_mismatch' in NOWPAYMENTS_FINAL_STATUSES
