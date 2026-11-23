import pytest
from app.localization.texts import get_texts


@pytest.mark.parametrize('key', [
    'CABINET_RENEWAL_CONFLICTING_IDS', 'CABINET_RENEWAL_OPERATION_CHANGED',
    'CABINET_RENEWAL_SUBSCRIPTION_CHANGED', 'TRAFFIC_PURCHASE_NOT_APPLIED',
    'TRANSACTION_SWITCH_DAILY_FIRST_DAY',
])
def test_new_general_messages_resolve_from_persian_catalog(key):
    import json
    from pathlib import Path
    catalog = json.loads(Path('app/localization/locales/fa.json').read_text(encoding='utf-8'))
    assert get_texts('fa').t(key, 'UNTRANSLATED') == catalog[key]
    assert 'UNTRANSLATED' not in catalog[key].format(tariff='ویژه')
