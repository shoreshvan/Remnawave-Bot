from app.localization.texts import get_texts
from app.utils.transaction_description import localize_transaction_description


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


def test_daily_price_has_grouping_without_decimal_zeros():
    assert get_texts('fa').t('SUBSCRIPTION_UI_DAILY_PRICE').format(price=20000.0) == 'قیمت: 20,000 تومان/روز'
