"""Persian entry points must render real catalog text, never a default-language fallback."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import menu, start
from app.services.registration_access_service import RegistrationAccessDecision, RegistrationAccessReason
from scripts.audit_persian_localization import load_catalog, scan_file
from tests.fixtures.persian import persian  # noqa: F401


def assert_persian(value):
    assert re.search(r'[\u0600-\u06ff]', value), value
    assert not re.search(r'[А-Яа-яЁё]', value), value


@pytest.mark.parametrize('filename', ['start.py', 'menu.py'])
def test_entry_point_static_keys_resolve_without_fallback(persian, filename):
    root = Path(__file__).resolve().parents[2]
    catalog, duplicates, collisions = load_catalog(root / 'app/localization/locales/fa.json')
    assert not duplicates and not collisions
    findings, keys = scan_file(root / 'app/handlers' / filename, catalog)
    assert not [item for item in findings if item['kind'] in {'missing_fa_key', 'invalid_fa_value'}]
    for key in keys - {'RULES_TEXT'}:
        assert isinstance(persian.t(key), str)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', [
    RegistrationAccessReason.INVITE_REQUIRED,
    RegistrationAccessReason.CHECK_UNAVAILABLE,
    RegistrationAccessReason.BLOCKED,
])
async def test_registration_denial_and_support_button(persian, monkeypatch, reason):
    monkeypatch.setattr(type(start.settings), 'get_support_contact_url', lambda self: 'https://t.me/support')
    answer = AsyncMock()
    await start._answer_registration_denial(answer, persian, RegistrationAccessDecision(False, reason))
    assert_persian(answer.await_args.args[0])
    button = answer.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert_persian(button.text)
    assert button.url == 'https://t.me/support'


@pytest.mark.asyncio
async def test_first_language_prompt_uses_telegram_language(persian, monkeypatch):
    monkeypatch.setattr(start.settings, 'DEFAULT_LANGUAGE', 'ru')
    message = SimpleNamespace(from_user=SimpleNamespace(id=7, language_code='fa-IR'), answer=AsyncMock())
    state = SimpleNamespace(get_data=AsyncMock(return_value={}), set_state=AsyncMock())
    await start._prompt_language_selection(message, state)
    assert message.answer.await_args.args[0] == persian.t('LANGUAGE_PROMPT')
    # Language names remain native so users can recognize their own language.
    buttons = message.answer.await_args.kwargs['reply_markup'].inline_keyboard
    assert any('فارسی' in button.text for row in buttons for button in row)


@pytest.mark.asyncio
@pytest.mark.parametrize('registered', [True, False])
async def test_unsupported_language_uses_existing_context(persian, monkeypatch, registered):
    monkeypatch.setattr(type(start.settings), 'is_language_selection_enabled', lambda self: True)
    monkeypatch.setattr(type(start.settings), 'get_available_languages', lambda self: ['ru', 'en', 'fa'])
    callback = SimpleNamespace(data='language_select:invalid', answer=AsyncMock(),
                               from_user=SimpleNamespace(id=7, language_code='en'))
    if registered:
        await menu.process_language_change(callback, SimpleNamespace(language='fa'), object())
    else:
        state = SimpleNamespace(get_data=AsyncMock(return_value={'language': 'fa'}))
        await start.process_language_selection(callback, state, object())
    callback.answer.assert_awaited_once_with(persian.t('LANGUAGE_UNSUPPORTED'), show_alert=True)


@pytest.mark.asyncio
async def test_missing_user_error_uses_telegram_persian(persian):
    callback = SimpleNamespace(from_user=SimpleNamespace(language_code='fa_IR'), answer=AsyncMock())
    await menu.show_main_menu(callback, None, object())
    callback.answer.assert_awaited_once_with(persian.t('USER_NOT_FOUND_ERROR'), show_alert=True)


@pytest.mark.asyncio
async def test_profile_error_prefers_saved_language(persian):
    callback = SimpleNamespace(from_user=SimpleNamespace(language_code='en'), answer=AsyncMock())
    await menu.handle_profile_unavailable(callback, SimpleNamespace(language='fa'))
    callback.answer.assert_awaited_once_with(persian.t('MENU_PROFILE_UNAVAILABLE'), show_alert=True)


@pytest.mark.asyncio
async def test_declining_rules_without_state_language_is_persian(persian):
    callback = SimpleNamespace(data='rules_decline', from_user=SimpleNamespace(id=7, language_code='fa'),
                               answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    state = SimpleNamespace(get_state=AsyncMock(return_value=None), get_data=AsyncMock(return_value={}))
    await start.process_rules_accept(callback, state, object())
    assert callback.message.edit_text.await_args.args[0] == persian.t('RULES_REQUIRED')
    for row in callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard:
        for button in row:
            assert_persian(button.text)


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [*start._COUPON_ERROR_TEXTS, 'future_provider_code', None])
async def test_coupon_errors_use_recipient_language(persian, monkeypatch, code):
    error = start.CouponRedemptionError(code) if code else RuntimeError('provider error')
    monkeypatch.setattr(start, 'redeem_coupon', AsyncMock(side_effect=error))
    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_coupon_token': 'a' * 32}))
    answer = AsyncMock()
    await start._redeem_pending_coupon(object(), state, SimpleNamespace(id=7, language='fa'), answer)
    expected = code if code in start._COUPON_ERROR_KEYS else ('internal' if code is None else 'invalid')
    assert answer.await_args.args[0] == persian.t(start._COUPON_ERROR_KEYS[expected])
    assert_persian(answer.await_args.args[0])


@pytest.mark.asyncio
async def test_coupon_success_preserves_days_and_escapes_tariff(persian, monkeypatch):
    monkeypatch.setattr(
        start, 'redeem_coupon', AsyncMock(return_value=SimpleNamespace(tariff_name='طرح <ویژه>', period_days=30))
    )
    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_coupon_token': 'a' * 32}))
    answer = AsyncMock()
    await start._redeem_pending_coupon(object(), state, SimpleNamespace(id=7, language='fa'), answer)
    assert answer.await_args.args[0] == persian.t('COUPON_REDEEMED').format(tariff='طرح &lt;ویژه&gt;', days=30)


@pytest.mark.asyncio
async def test_coupon_confirmation_failure_does_not_claim_redemption_failed(persian, monkeypatch):
    redeem = AsyncMock(return_value=SimpleNamespace(tariff_name='طرح', period_days=30))
    monkeypatch.setattr(start, 'redeem_coupon', redeem)
    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_coupon_token': 'a' * 32}))
    answer = AsyncMock(side_effect=RuntimeError('Telegram unavailable'))
    await start._redeem_pending_coupon(object(), state, SimpleNamespace(id=7, language='fa'), answer)
    assert answer.await_count == 1
    assert redeem.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['active', 'trial', 'expired', 'disabled', 'limited'])
@pytest.mark.parametrize('named', [True, False])
async def test_multi_tariff_status_is_persian(persian, monkeypatch, status, named):
    from app.database.crud import subscription

    sub = SimpleNamespace(tariff=SimpleNamespace(name='طرح <ویژه>') if named else None,
                          actual_status=status, is_pending_trial=False,
                          end_date=datetime.now(UTC) + timedelta(days=5))
    monkeypatch.setattr(subscription, 'get_all_subscriptions_by_user_id', AsyncMock(return_value=[sub]))
    result, _ = await menu._get_multi_tariff_status(SimpleNamespace(id=7), persian, object())
    assert_persian(result)
    assert ('طرح &lt;ویژه&gt;' if named else 'اشتراک') in result
    if status in {'active', 'trial'}:
        assert 'روز' in result
    else:
        expected = {'expired': 'منقضی‌شده', 'disabled': 'غیرفعال', 'limited': 'ترافیک تمام شده'}[status]
        assert expected in result


@pytest.mark.asyncio
async def test_single_tariff_menu_includes_translated_tariff_block(persian, monkeypatch):
    from app.database.crud import tariff

    monkeypatch.setattr(type(menu.settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(menu.settings), 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(
        tariff, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(name='طرح <ویژه>', is_daily=False))
    )
    monkeypatch.setattr(menu, 'build_promo_offer_hint', AsyncMock(return_value=''))
    monkeypatch.setattr(menu, 'build_test_access_hint', AsyncMock(return_value=''))
    monkeypatch.setattr(menu, 'get_random_active_message', AsyncMock(return_value=None))
    sub = SimpleNamespace(tariff_id=1, actual_status='active', is_trial=False,
                          end_date=datetime.now(UTC) + timedelta(days=10))
    user = SimpleNamespace(id=7, full_name='کاربر <آزمایشی>', subscription=sub)
    result = await menu.get_main_menu_text(user, persian, object())
    assert_persian(result)
    assert '📦 تعرفه: طرح &lt;ویژه&gt;' in result
    assert 'کاربر &lt;آزمایشی&gt;' in result
    assert result.count(persian.t('MAIN_MENU_ACTION_PROMPT')) == 1


@pytest.mark.parametrize('admin', [False, True])
def test_command_descriptions_resolve_all_dynamic_keys(persian, monkeypatch, admin):
    from app.services.bot_commands import build_menu

    monkeypatch.setattr(start.settings, 'TELEGRAM_STARS_ENABLED', True)
    commands = build_menu('fa', admin=admin)
    assert commands[0].command == 'start'
    assert any(command.command == 'paysupport' for command in commands)
    for command in commands:
        assert_persian(command.description)


@pytest.mark.asyncio
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('has_subscription', [False, True])
async def test_rich_menu_renders_persian_without_fallback(persian, monkeypatch, multi, has_subscription):
    from app.utils import rich_menu

    monkeypatch.setattr(type(menu.settings), 'is_multi_tariff_enabled', lambda self: multi)
    monkeypatch.setattr(type(menu.settings), 'is_tariffs_mode', lambda self: False)
    for name in ('build_promo_offer_hint', 'build_test_access_hint', 'get_random_active_message'):
        monkeypatch.setattr(rich_menu, name, AsyncMock(return_value=None))
    now = datetime.now(UTC)
    sub = SimpleNamespace(
        id=7, actual_status='active', is_trial=False, is_pending_trial=False,
        start_date=now - timedelta(days=20), end_date=now + timedelta(days=10),
        tariff_id=None, tariff=SimpleNamespace(name='طرح آزمایشی'), traffic_used_gb=12,
        traffic_limit_gb=100, device_limit=3, subscription_url=None, subscription_crypto_link=None,
    ) if has_subscription else None
    user = SimpleNamespace(
        id=7, full_name='کاربر <آزمایشی>', language='fa', auth_type='telegram', balance_kopeks=125000,
        subscription=sub, subscriptions=[sub] if sub else [], is_trial_already_used=lambda: True,
    )
    monkeypatch.setattr(rich_menu, 'get_all_subscriptions_by_user_id', AsyncMock(return_value=user.subscriptions))
    result = await rich_menu.build_main_menu_rich_html(user, persian, object())
    assert_persian(result)
    assert 'کاربر &lt;آزمایشی&gt;' in result
    assert persian.t('MAIN_MENU_ACTION_PROMPT') in result
    assert rich_menu._input_rich_message(result, 'fa').is_rtl is True
