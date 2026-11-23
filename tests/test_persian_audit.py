"""The inventory must find indirect display text without mistaking dicts for catalogs."""

from scripts import audit_persian_localization as audit


def test_scanner_tracks_lookup_forms_and_indirect_text(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, 'ROOT', tmp_path)
    path = tmp_path / 'sample.py'
    path.write_text('''
def render(texts, callback):
    """Внутренняя документация."""
    logger.info('Внутренний журнал')
    label = 'Подписка'
    callback.answer(label)
    callback.answer('Unsupported language')
    texts.t('TITLE', 'Заголовок')
    texts.get('MISSING', 'Запасной текст')
    texts.BACK
    texts['CAPTION']
    _t(texts, 'WRAPPED', 'Пример')
    texts.t(f'STATE_{status}')
def dictionary_button(texts: dict[str, str]):
    return texts.get('fa', texts['en'])
def local_dictionary():
    texts = {'fa': 'فارسی', 'en': 'English'}
    return texts.get('fa', texts['en'])
''', encoding='utf-8')
    findings, keys = audit.scan_file(path, {'TITLE': 'عنوان', 'BACK': 'بازگشت', 'CAPTION': 'متن', 'WRAPPED': 'نمونه'})
    assert keys == {'TITLE', 'MISSING', 'BACK', 'CAPTION', 'WRAPPED'}
    assert [item['key'] for item in findings if item['kind'] == 'missing_fa_key'] == ['MISSING']
    assert [item['text'] for item in findings if item['kind'] == 'indirect_cyrillic_candidate'] == ['Подписка']
    assert any(item['kind'] == 'direct_foreign_display' for item in findings)
    assert any(item['kind'] == 'dynamic_key' for item in findings)


def test_catalog_loader_detects_duplicate_and_runtime_normalized_collisions(tmp_path):
    path = tmp_path / 'fa.json'
    path.write_text('{"a": "اول", "a": "دوم", "A": "سوم", "group": {"title": "عنوان"}}', encoding='utf-8')
    catalog, duplicates, collisions = audit.load_catalog(path)
    assert duplicates == ['a']
    assert collisions == ['A']
    assert catalog['GROUP_TITLE'] == 'عنوان'
