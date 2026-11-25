import pytest

from app.localization.texts import get_texts
from app.utils.transaction_description import localize_transaction_description
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.parametrize('method', ['atlaspay', 'hooshpay', 'tonpays', 'nowpayments'])
def test_gateway_deposit_preserves_merchant_label(method, persian):
    assert localize_transaction_description('Пополнение через فروشگاه من', 'fa', method) == 'شارژ از طریق فروشگاه من'


def test_legacy_daily_purchase_preserves_tariff_name():
    assert localize_transaction_description(
        'Покупка суточного тарифа روزانه (پیشرفته) (первый день)', 'fa'
    ) == 'خرید تعرفهٔ روزانهٔ «روزانه (پیشرفته)» (روز اول)'


def test_unrelated_and_empty_descriptions_unchanged():
    for value in (None, '', 'پرداخت دستی', 'Покупка другого тарифа'):
        assert localize_transaction_description(value, 'fa') == value


def test_russian_daily_purchase_unchanged():
    value = 'Покупка суточного тарифа Test (первый день)'
    assert localize_transaction_description(value, 'ru') == value


def test_old_cabinet_charge_shows_plan_and_both_discounts(persian):
    stored = "Покупка тарифа 'ویژه' на 30 дней (скидка 10%) (промо -5%)"
    assert localize_transaction_description(stored, 'fa') == (
        'خرید تعرفهٔ «ویژه» به مدت 30 روز'
        + persian.t('TARIFF_PURCHASE_DISCOUNT_SUFFIX').format(percent=10)
        + persian.t('TARIFF_PURCHASE_PROMO_SUFFIX').format(percent=5)
    )


@pytest.mark.parametrize(('stored', 'expected'), [
    ("Смена тарифа администратором на 'طرح O'Brien'", "تغییر تعرفه توسط مدیر به «طرح O'Brien»"),
    ('Покупка тарифа طرح на 10 на 30 дней (администратор)', 'خرید تعرفهٔ «طرح на 10» به مدت 30 روز (توسط مدیر)'),
])
def test_admin_tariff_history_preserves_plan_name(stored, expected, persian):
    assert localize_transaction_description(stored, 'fa') == expected


def test_daily_price_has_grouping_without_decimal_zeros():
    assert get_texts('fa').t('SUBSCRIPTION_UI_DAILY_PRICE').format(price=20000.0) == 'قیمت: 20,000 تومان/روز'


@pytest.mark.parametrize(('suffix', 'expected'), [
    ('', 'پرداخت روزانهٔ تعرفهٔ «ویژه»'),
    (' (возобновление)', 'پرداخت روزانهٔ تعرفهٔ «ویژه» (از سرگیری)'),
    (' (авто-возобновление)', 'پرداخت روزانهٔ تعرفهٔ «ویژه» (ازسرگیری خودکار)'),
])
def test_daily_debit_history_in_persian(suffix, expected, persian):
    assert localize_transaction_description('Суточная оплата тарифа «ویژه»' + suffix, 'fa') == expected


def test_old_traffic_topup_receipts(persian):
    stored = 'Докупка 10 ГБ трафика'
    assert localize_transaction_description(stored, 'fa') == 'خرید 10 گیگابایت ترافیک'
    assert localize_transaction_description(stored + ' (скидка 12.5%)', 'fa') == 'خرید 10 گیگابایت ترافیک (تخفیف 12.5٪)'
