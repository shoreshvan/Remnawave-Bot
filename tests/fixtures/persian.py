"""Use real Persian catalog values and fail if code attempts any language fallback."""

import pytest

from app.localization import loader, texts as localization


@pytest.fixture
def persian(monkeypatch, tmp_path):
    monkeypatch.setattr(loader.settings, 'LOCALES_PATH', str(tmp_path))
    monkeypatch.setattr(localization, 'DEFAULT_LANGUAGE', 'ru')
    monkeypatch.setattr(loader, 'DEFAULT_LANGUAGE', 'ru')
    loader.clear_locale_cache()
    original = localization.Texts._get_value

    def strict_lookup(self, key, warn=True):
        assert self.language == 'fa', f'Unexpected recipient language: {self.language}'
        assert key in self._values, f'Persian output depended on fallback: {key}'
        return original(self, key, warn=warn)

    monkeypatch.setattr(localization.Texts, '_get_value', strict_lookup)
    yield localization.get_texts('fa')
    loader.clear_locale_cache()
