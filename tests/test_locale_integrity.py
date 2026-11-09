"""Catalog integrity with complete Persian coverage independent of other locales.

Persian may contain extra keys; the existing non-Persian catalogs retain parity.
Missing Persian lookups and formatting errors remain failures.
"""

import ast
import re
from functools import cache
from pathlib import Path
from string import Formatter

import pytest


LOCALE_DIR = Path(__file__).resolve().parents[1] / 'app' / 'localization' / 'locales'
APP_DIR = Path(__file__).resolve().parents[1] / 'app'
LANGS = ['ru', 'en', 'ua', 'fa', 'zh']
PLACEHOLDER_RE = re.compile(r'\{[^}]*\}')
# Keys resolved dynamically in Texts._get_value (not stored in the JSON files).
DYNAMIC_KEYS = {'RULES_TEXT'}


@pytest.fixture(scope='module')
def locales():
    from scripts.audit_persian_localization import load_catalog

    result = {}
    for lang in LANGS:
        catalog, duplicates, collisions = load_catalog(LOCALE_DIR / f'{lang}.json')
        assert not duplicates and not collisions, (lang, duplicates, collisions)
        result[lang] = catalog
    return result


def test_catalog_key_coverage_allows_persian_extensions(locales):
    ru_keys = set(locales['ru'])
    for lang in LANGS:
        if lang == 'ru':
            continue
        keys = set(locales[lang])
        missing = sorted(ru_keys - keys)
        extra = sorted(keys - ru_keys)
        assert not missing, f'{lang}.json is missing {len(missing)} keys present in ru: {missing[:15]}'
        if lang != 'fa':
            assert not extra, f'{lang}.json has {len(extra)} keys not in ru: {extra[:15]}'


def test_placeholders_consistent_across_locales(locales):
    """Every {placeholder} must be identical across languages — the code calls
    .format() on the result, so a drifted placeholder breaks at runtime."""
    ru = locales['ru']
    problems = []
    for key, ru_val in ru.items():
        if not isinstance(ru_val, str):
            continue
        ru_ph = sorted(PLACEHOLDER_RE.findall(ru_val))
        for lang in LANGS:
            if lang == 'ru':
                continue
            val = locales[lang].get(key)
            if not isinstance(val, str):
                continue
            ph = sorted(PLACEHOLDER_RE.findall(val))
            if key == 'SUBSCRIPTION_UI_DAILY_PRICE' and lang == 'fa':
                # Intentional Persian whole-unit price presentation; the amount
                # and argument identity remain unchanged.
                assert ru_ph == ['{price:.2f}'] and ph == ['{price:,.0f}']
                assert '12,345' in val.format(price=12345)
                continue
            if ph != ru_ph:
                problems.append(f'{key} [{lang}]: ru={ru_ph} vs {lang}={ph}')
    assert not problems, 'Placeholder mismatches (would break .format()):\n' + '\n'.join(problems[:25])


@cache
def _iter_t_calls():
    calls = []
    for path in APP_DIR.rglob('*.py'):
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 't'
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                calls.append((path, node))
    return tuple(calls)


def test_t_calls_without_default_exist_in_ru(locales):
    """texts.t('KEY') with NO fallback raises KeyError if the key is absent from ru.
    Every such referenced key must exist in ru.json."""
    ru = locales['ru']
    offenders = set()
    for path, node in _iter_t_calls():
        if len(node.args) != 1:
            continue
        key = node.args[0].value
        if key not in DYNAMIC_KEYS and key not in ru:
            offenders.add(f'{key}  ({path.relative_to(APP_DIR.parent)})')
    assert not offenders, 'texts.t(key) with no fallback for a key missing from ru.json:\n' + '\n'.join(
        sorted(offenders)[:25]
    )


def test_all_static_t_keys_exist_in_persian(locales):
    """Even calls with inline/dynamic defaults must resolve their own Persian key."""
    fa = locales['fa']
    offenders = set()
    for path, node in _iter_t_calls():
        key = node.args[0].value
        if key not in DYNAMIC_KEYS and key not in fa:
            offenders.add(f'{key}  ({path.relative_to(APP_DIR.parent)})')
    assert not offenders, 'Missing Persian keys:\n' + '\n'.join(sorted(offenders)[:25])


def test_persian_placeholders_are_supplied_to_direct_format_calls(locales):
    """Validate real keyword arguments, not defaults that may intentionally differ."""
    problems = []
    for path in APP_DIR.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'format' and isinstance(node.func.value, ast.Call)
            ):
                continue
            lookup = node.func.value
            if not (
                isinstance(lookup.func, ast.Attribute) and lookup.func.attr == 't'
                and lookup.args and isinstance(lookup.args[0], ast.Constant)
            ):
                continue
            key = lookup.args[0].value
            if key not in locales['fa'] or node.args or any(kw.arg is None for kw in node.keywords):
                continue
            supplied = {kw.arg for kw in node.keywords}
            required = {
                re.split(r'[.\[]', field)[0]
                for _, field, _, _ in Formatter().parse(locales['fa'][key]) if field is not None
            }
            if required - supplied:
                problems.append(f'{key}: {required - supplied} ({path.relative_to(APP_DIR.parent)}:{node.lineno})')
    assert not problems, '\n'.join(problems[:25])


@pytest.mark.parametrize(
    'key',
    [
        'REGISTRATION_INVITE_REQUIRED',
        'REGISTRATION_CHECK_UNAVAILABLE',
        'REGISTRATION_CONTACT_SUPPORT',
    ],
)
def test_invite_only_keys_exist_in_every_locale(locales, key):
    assert all(key in locales[lang] for lang in LANGS)


def test_admin_keyboard_wrapper_keys_exist_in_all_locales(locales):
    """The _t(texts, key, fallback) wrapper must not bypass the locale guard."""
    path = APP_DIR / 'keyboards' / 'admin.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    keys = {
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == '_t'
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    }
    from app.keyboards.admin import BROADCAST_BUTTONS

    keys.update(config['text_key'] for config in BROADCAST_BUTTONS.values())
    for language in LANGS:
        assert not (keys - locales[language].keys()), (language, sorted(keys - locales[language].keys()))
