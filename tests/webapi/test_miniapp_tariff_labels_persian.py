from types import SimpleNamespace

import pytest

from app.webapi.routes import miniapp
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('gb', 'label'), [(0, '♾️ نامحدود'), (15, '15 گیگابایت')])
async def test_tariff_labels_without_account_language(persian, gb, label):
    tariff = SimpleNamespace(
        id=1, name='ویژه', description=None, allowed_squads=[], tier_level=1, traffic_limit_gb=gb,
        device_limit=2, is_active=True, is_daily=True, daily_price_kopeks=12345,
        period_prices={'30': 100000}, get_price_for_period=lambda days: 100000,
    )
    listed = await miniapp._build_tariff_model(None, tariff)
    current = await miniapp._build_current_tariff_model(None, tariff)
    for item in (listed, current):
        assert item.traffic_limit_label == label
        assert item.daily_price_label == '123.45 تومان/روز'
        assert item.daily_price_kopeks == 12345
    assert listed.periods[0].label == '1 ماه'
