"""Юниты трафика должны брать язык из Texts, а не из зашитого 'ru'.

Раньше format_traffic был staticmethod с language='ru' по умолчанию, поэтому
fa-пользователи видели «ГБ» (например, в карточке пробной подписки).
"""

from app.localization.texts import Texts


def test_format_traffic_uses_instance_language():
    assert Texts('fa').format_traffic(10) == '10 گیگ'
    assert Texts('ru').format_traffic(10) == '10 ГБ'


def test_format_traffic_explicit_language_wins():
    assert Texts('ru').format_traffic(10, language='fa') == '10 گیگ'
