"""Real Persian keyboard labels preserve prices, selections and callback payloads."""

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.keyboards import inline
from tests.fixtures.persian import persian  # noqa: F401


def buttons(markup):
    result = {button.callback_data: button.text for row in markup.inline_keyboard for button in row}
    for label in result.values():
        assert not re.search(r'[А-Яа-яЁё]', label), label
    return result


@pytest.fixture
def frozen_clock(monkeypatch):
    now = datetime(2030, 1, 1, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(inline, 'datetime', Clock)
    return now


def test_purchase_packages_and_empty_state(persian, monkeypatch):
    monkeypatch.setattr(type(inline.settings), 'is_traffic_topup_blocked', lambda self: False)
    packages = [{'gb': 0, 'price': 12000, 'enabled': True}, {'gb': 50, 'price': 6000, 'enabled': True}]
    monkeypatch.setattr(type(inline.settings), 'get_traffic_packages', lambda self: packages)
    result = buttons(inline.get_traffic_packages_keyboard('fa'))
    assert result['traffic_0'] == f'♾️ نامحدود - {inline.settings.format_price(12000)}'
    assert result['traffic_50'] == f'📊 50 گیگابایت - {inline.settings.format_price(6000)}'
    packages.clear()
    assert 'no_traffic_packages' in buttons(inline.get_traffic_packages_keyboard('fa'))


@pytest.mark.parametrize('days', [1, 15, 60])
def test_topup_period_price_and_discount(persian, monkeypatch, frozen_clock, days):
    packages = [{'gb': 0, 'price': 30000, 'enabled': True}, {'gb': 50, 'price': 30000, 'enabled': True}]
    monkeypatch.setattr(type(inline.settings), 'get_traffic_topup_packages', lambda self: packages)
    monkeypatch.setattr(type(inline.settings), 'is_multi_tariff_enabled', lambda self: True)
    result = buttons(inline.get_add_traffic_keyboard('fa', frozen_clock + timedelta(days=days), 10, sub_id=7))
    price, discount = int(27000 * days / 30) // 100, int(3000 * days / 30) // 100
    for key in ('add_traffic_0', 'add_traffic_50'):
        assert f'{price} ₽' in result[key]
        assert f'تخفیف 10٪: -{discount}₽' in result[key]
        assert ('برای ۱ روز' if days == 1 else f'برای {days} روز') in result[key]
    assert 'sm:7' in result


def test_tariff_topup_remains_monthly_regardless_of_subscription_length(persian, frozen_clock):
    short = buttons(inline.get_add_traffic_keyboard_from_tariff(
        'fa', {50: 10000}, frozen_clock + timedelta(days=1), 10
    ))
    long = buttons(inline.get_add_traffic_keyboard_from_tariff(
        'fa', {50: 10000}, frozen_clock + timedelta(days=180), 10
    ))
    assert short == long
    assert short['add_traffic_50'] == '📊 +50 گیگابایت ترافیک - 90 ₽ /ماه (تخفیف 10٪: -10₽)'


def test_autopay_days_and_periods_are_persian(persian):
    result = buttons(inline.get_autopay_days_keyboard('fa'))
    assert result['autopay_days_1'] == '1 روز'
    assert result['autopay_days_14'] == '14 روز'
    periods = buttons(inline.get_autopay_period_keyboard([30, 90], 90, 'fa'))
    assert periods['autopay_period_90'] == '✅ 90 روز'
    assert 'autopay_period_default' in periods


def test_device_choices_include_current_free_paid_and_no_refund(persian, monkeypatch, frozen_clock):
    monkeypatch.setattr(inline.settings, 'DEFAULT_DEVICE_LIMIT', 2)
    monkeypatch.setattr(inline.settings, 'PRICE_PER_DEVICE', 3000)
    monkeypatch.setattr(inline.settings, 'MAX_DEVICES_LIMIT', 8)
    monkeypatch.setattr(inline.settings, 'ALLOW_DEVICES_BELOW_TARIFF_LIMIT', True)
    result = buttons(inline.get_change_devices_keyboard(3, 'fa', frozen_clock + timedelta(days=30), 10))
    assert result['change_devices_3'] == '✅ 3 دستگاه (فعلی)'
    assert 'بدون بازگشت وجه' in result['change_devices_2']
    assert '27₽' in result['change_devices_4'] and 'تخفیف 10٪: -3₽' in result['change_devices_4']
    free = buttons(inline.get_change_devices_keyboard(1, 'fa'))
    assert 'رایگان' in free['change_devices_2']
    initial = buttons(inline.get_devices_keyboard(2, 'fa'))
    assert 'شامل اشتراک' in initial['devices_2']


def test_country_choices_and_apply_totals(persian, monkeypatch, frozen_clock):
    monkeypatch.setattr(type(inline.settings), 'is_multi_tariff_enabled', lambda self: True)
    countries = [{'uuid': 'a', 'name': 'سرور اول', 'price_kopeks': 30000},
                 {'uuid': 'b', 'name': 'سرور دوم', 'price_kopeks': 0}]
    result = buttons(inline.get_manage_countries_keyboard(
        countries, ['a'], [], 'fa', frozen_clock + timedelta(days=60), 10, sub_id=7
    ))
    assert '270₽/ماه × 60 روز = 540₽' in result['country_manage_a']
    assert 'تخفیف 10٪: -60₽' in result['country_manage_a']
    assert result['countries_apply'] == '✅ اعمال تغییرات (540 ₽)'
    assert 'sm:7' in result
    free = buttons(inline.get_manage_countries_keyboard(countries, ['b'], [], 'fa'))
    assert free['countries_apply'] == '✅ اعمال تغییرات'
    initial = buttons(inline.get_countries_keyboard(countries, [], 'fa'))
    assert 'رایگان' in initial['country_b']


def test_card_mask_and_device_alias_are_preserved(persian):
    card = SimpleNamespace(method_type='bank_card', title=None, card_last4='1234', card_type=None)
    assert inline._get_payment_method_display_name(card, 'fa') == 'کارت *1234'
    pagination = SimpleNamespace(page=1, total_pages=1)
    result = buttons(inline.get_devices_management_keyboard([{}, {'local_name': 'گوشی من'}], pagination, 'fa'))
    assert result['reset_device_0_1'] == '🔄 نامشخص - نامشخص'
    assert result['reset_device_1_1'] == '🔄 گوشی من'


def test_anore_method_labels_preserve_payment_payloads(persian, monkeypatch):
    monkeypatch.setattr(type(inline.settings), 'is_anore_enabled', lambda self: True)
    monkeypatch.setattr(type(inline.settings), 'get_anore_methods', lambda self: ['sbp', 'card', 'yoomoney', 'crypto'])
    monkeypatch.setattr(type(inline.settings), 'get_anore_display_name', lambda self: 'Anore')
    result = buttons(inline.get_payment_methods_keyboard(12345, 'fa'))
    for method, label in {'sbp': 'پرداخت سریع', 'card': 'کارت', 'yoomoney': 'YooMoney', 'crypto': 'رمزارز'}.items():
        assert result[f'topup_amount|anore_{method}|12345'] == f'💳 Anore — {label}'


def test_ticket_defaults_and_operator_text(persian):
    tickets = [{'id': 7}, {'id': 8, 'title': 'عنوان انتخابی', 'user_name': 'نام انتخابی'}]
    user = buttons(inline.get_my_tickets_keyboard(tickets, language='fa'))
    admin = buttons(inline.get_admin_tickets_keyboard(tickets, language='fa'))
    assert 'بدون عنوان' in user['view_ticket_7']
    assert 'بدون عنوان' in admin['admin_view_ticket_7']
    assert 'کاربر ناشناس' in admin['admin_view_ticket_7']
    assert 'عنوان انتخابی' in user['view_ticket_8']
    assert 'نام انتخابی' in admin['admin_view_ticket_8']
