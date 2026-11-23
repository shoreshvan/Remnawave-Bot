"""Actual Persian payment screens with fake invoice creation and Telegram IO."""

import re
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.balance import atlaspay, hooshpay, nowpayments, tonpays
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['atlaspay', 'hooshpay', 'nowpayments', 'tonpays'])
@pytest.mark.parametrize('succeeded', [False, True])
async def test_invoice_creation_screen_and_failure(persian, monkeypatch, provider, succeeded):
    module = {'atlaspay': atlaspay, 'hooshpay': hooshpay, 'nowpayments': nowpayments, 'tonpays': tonpays}[provider]
    result = dict(payment_url='https://pay.example/invoice', invoice_id='invoice', order_id='order-test',
                  tracking_code='tracking-test', card_number='1234567890123456', card_name='نام صاحب کارت',
                  total_amount_kopeks=260000, payable_amount_kopeks=260000, final_amount_kopeks=260000)
    create = AsyncMock(return_value=result if succeeded else None)
    monkeypatch.setattr(module, 'PaymentService', lambda: SimpleNamespace(**{f'create_{provider}_payment': create}))
    monkeypatch.setattr(module.settings, 'PAYMENT_BALANCE_TEMPLATE', '{description}')
    user = SimpleNamespace(id=7, telegram_id=77, language='fa')
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    kwargs = dict(message_or_callback=message, db_user=user, db=AsyncMock(), amount_kopeks=250000)
    if provider == 'tonpays':
        kwargs['state'] = AsyncMock()
    await getattr(module, f'_create_{provider}_payment_and_respond')(**kwargs)
    assert create.await_args.kwargs['description'] == 'شارژ موجودی'
    assert create.await_args.kwargs['language'] == 'fa'
    assert create.await_args.kwargs['amount_kopeks'] == 250000
    text = message.answer.await_args.args[0]
    assert re.search('[А-Яа-яЁё]', text) is None
    assert 'Balance top-up' not in text and '₽' not in text
    if succeeded:
        assert ('2٬500' if provider == 'nowpayments' else '2٬600') in text
        assert 'تومان' in text
        for row in message.answer.await_args.kwargs['reply_markup'].inline_keyboard:
            for button in row:
                assert re.search('[А-Яа-яЁё]', button.text) is None
    else:
        assert text == persian.t('PAYMENT_CREATE_ERROR')


@pytest.mark.asyncio
@pytest.mark.parametrize('after_download', [False, True])
@pytest.mark.parametrize('limit', [1024 * 1024, 512 * 1024])
async def test_tonpays_receipt_limit_message_tracks_configuration(persian, monkeypatch, after_download, limit):
    monkeypatch.setattr(tonpays.settings, 'TONPAYS_RECEIPT_MAX_BYTES', limit)
    message = SimpleNamespace(
        photo=[SimpleNamespace(file_id='photo', file_size=None if after_download else limit + 1)],
        answer=AsyncMock(), bot=SimpleNamespace(download=AsyncMock(return_value=BytesIO(b'x' * (limit + 1)))),
    )
    state = SimpleNamespace(get_data=AsyncMock(return_value={'tonpays_order_id': 'order-test'}))
    await tonpays.process_tonpays_receipt(message, SimpleNamespace(language='fa'), AsyncMock(), state)
    message.answer.assert_awaited_once_with(
        f'حجم فیش بیش از حد مجاز است (حداکثر {limit / (1024 * 1024):g} مگابایت).'
    )
    assert message.bot.download.await_count == int(after_download)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['atlaspay', 'nowpayments', 'tonpays'])
@pytest.mark.parametrize(('status', 'suffix', 'clears'), [
    (None, 'ERROR', False), ('paid', 'PAID', True), ('expired', 'EXPIRED', True),
    ('failed', 'FAILED', True), ('pending', 'PENDING', False), ('unknown_future_state', 'PENDING', False),
    ('amount_mismatch', 'MISMATCH', False),
])
async def test_payment_status_alerts(persian, monkeypatch, provider, status, suffix, clears):
    module = {'atlaspay': atlaspay, 'nowpayments': nowpayments, 'tonpays': tonpays}[provider]
    result = None if status is None else {'status': status, 'is_paid': status == 'paid'}
    check = AsyncMock(return_value=result)
    monkeypatch.setattr(module, 'PaymentService', lambda bot: SimpleNamespace(**{f'check_{provider}_payment_status': check}))
    prefix = getattr(module, f'_{provider.upper()}_CHECK_PREFIX')
    callback = SimpleNamespace(data=f'{prefix}order-test', bot=None, answer=AsyncMock())
    state, db = AsyncMock(), AsyncMock()
    await getattr(module, f'handle_{provider}_check')(callback, SimpleNamespace(language='fa'), db, state)
    key = 'PAYMENT_CHECK_AMOUNT_MISMATCH' if suffix == 'MISMATCH' else f'{provider.upper()}_CHECK_{suffix}'
    callback.answer.assert_awaited_once_with(persian.t(key), show_alert=True)
    check.assert_awaited_once_with(db, 'order-test')
    assert state.clear.await_count == int(clears)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['failed', 'cooldown', 'success', 'edit_failed'])
async def test_tonpays_card_change(persian, monkeypatch, outcome):
    payment = SimpleNamespace(final_amount_kopeks=260000, amount_kopeks=250000,
                              card_number='1234567890123456', card_name='<نام>')
    result = None if outcome == 'failed' else {'cooldown_wait': 45} if outcome == 'cooldown' else {'payment': payment}
    monkeypatch.setattr(tonpays, 'PaymentService', lambda bot: SimpleNamespace(change_tonpays_card=AsyncMock(return_value=result)))
    callback = SimpleNamespace(data='tonpays_card|order-test', bot=None, answer=AsyncMock(),
                               message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()))
    if outcome == 'edit_failed':
        callback.message.edit_text.side_effect = RuntimeError('cannot edit')
    await tonpays.handle_tonpays_new_card(callback, SimpleNamespace(language='fa'), AsyncMock(), AsyncMock())
    if outcome in ('failed', 'cooldown'):
        expected = persian.t('TONPAYS_NEW_CARD_ERROR') if outcome == 'failed' else '⏳ لطفاً 45 ثانیه صبر کنید.'
        callback.answer.assert_awaited_once_with(expected, show_alert=True)
    else:
        sent = callback.message.answer if outcome == 'edit_failed' else callback.message.edit_text
        text = sent.await_args.args[0]
        assert '2٬600 تومان' in text and '&lt;نام&gt;' in text
        assert '<code>1234567890123456</code>' in text
        buttons = sent.await_args.kwargs['reply_markup'].inline_keyboard
        assert buttons[0][0].callback_data == 'tonpays_check|order-test'
        assert buttons[1][0].text == '🔄 تعویض کارت'


@pytest.mark.asyncio
@pytest.mark.parametrize(('outcome', 'key'), [
    ('no_photo', 'TONPAYS_PHOTO_HINT'), ('download_failed', 'TONPAYS_RECEIPT_ERROR'),
    ('upload_failed', 'TONPAYS_RECEIPT_FAILED'), ('success', 'TONPAYS_RECEIPT_OK'),
])
async def test_tonpays_receipt_delivery(persian, monkeypatch, outcome, key):
    upload = AsyncMock(return_value=outcome == 'success')
    monkeypatch.setattr(tonpays, 'PaymentService', lambda bot: SimpleNamespace(upload_tonpays_receipt=upload))
    message = SimpleNamespace(text='فیش', answer=AsyncMock(),
        photo=[] if outcome == 'no_photo' else [SimpleNamespace(file_size=3, file_id='photo')],
        bot=SimpleNamespace(download=AsyncMock(return_value=BytesIO(b'img'))))
    if outcome == 'download_failed':
        message.bot.download.side_effect = RuntimeError('internal download detail')
    state = SimpleNamespace(get_data=AsyncMock(return_value={'tonpays_order_id': 'order-test'}))
    db = AsyncMock()
    await tonpays.process_tonpays_receipt(message, SimpleNamespace(language='fa', telegram_id=77), db, state)
    message.answer.assert_awaited_once_with(persian.t(key))
    if outcome in ('success', 'upload_failed'):
        upload.assert_awaited_once_with(db, 'order-test', file_bytes=b'img', filename='receipt.jpg')
    else:
        upload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['atlaspay', 'hooshpay', 'nowpayments', 'tonpays'])
@pytest.mark.parametrize('case', ['restricted', 'low', 'high', 'fraction', 'minimum', 'maximum'])
async def test_amount_and_account_validation(persian, monkeypatch, provider, case):
    module = {'atlaspay': atlaspay, 'hooshpay': hooshpay, 'nowpayments': nowpayments, 'tonpays': tonpays}[provider]
    monkeypatch.setattr(module.settings, f'{provider.upper()}_MIN_AMOUNT_KOPEKS', 10000)
    monkeypatch.setattr(module.settings, f'{provider.upper()}_MAX_AMOUNT_KOPEKS', 50000)
    create = AsyncMock()
    monkeypatch.setattr(module, f'_create_{provider}_payment_and_respond', create)
    amount = {'restricted': 20000, 'low': 9900, 'high': 50100, 'fraction': 10001,
              'minimum': 10000, 'maximum': 50000}[case]
    user = SimpleNamespace(language='fa', restriction_topup=case == 'restricted', restriction_reason=None)
    message, state = SimpleNamespace(answer=AsyncMock()), AsyncMock()
    await getattr(module, f'process_{provider}_payment_amount')(message, user, AsyncMock(), amount, state)
    if case in ('minimum', 'maximum'):
        assert create.await_args.kwargs['amount_kopeks'] == amount
        message.answer.assert_not_awaited()
        state.clear.assert_awaited_once()
    else:
        create.assert_not_awaited()
        text = message.answer.await_args.args[0]
        if case == 'low':
            assert text == persian.t('PAYMENT_AMOUNT_TOO_LOW').format(min_amount=100)
        elif case == 'high':
            assert text == persian.t('PAYMENT_AMOUNT_TOO_HIGH').format(max_amount=500)
        elif case == 'fraction':
            assert text == 'مبلغ باید به تومان کامل باشد (بدون خرده).'
        else:
            assert persian.t('SEVERPAY_RESTRICTION_DEFAULT_REASON') in text
        assert re.search('[А-Яа-яЁё]', text) is None
        assert state.clear.await_count == int(case == 'restricted')


@pytest.mark.asyncio
@pytest.mark.parametrize('entered', [None, '', 'abc', 'NaN', 'Infinity'])
async def test_shared_invalid_amount_is_persian(persian, entered):
    from app.handlers.balance.main import process_topup_amount

    message = SimpleNamespace(text=entered, successful_payment=None, answer=AsyncMock())
    await process_topup_amount(message, SimpleNamespace(language='fa'), AsyncMock())
    assert message.answer.await_args.args[0] == '❌ مبلغ نامعتبر'
