"""Actionable, exact RUB input limits for rejected Telegram top-ups."""

from app.config import settings
from app.localization.texts import get_texts


MIN_MANUAL_TOPUP_RUBLES = 1
MAX_MANUAL_TOPUP_RUBLES = 20000000


def _rubles(kopeks: int) -> str:
    whole, cents = divmod(kopeks, 100)
    return f'{whole}.{cents:02d}'.rstrip('0').rstrip('.') if cents else str(whole)


def format_topup_amount_retry(language: str, minimum: int, maximum: int, *, too_low: bool) -> str:
    texts = get_texts(language)
    lower = max(minimum, MIN_MANUAL_TOPUP_RUBLES * 100)
    upper = min(maximum, MAX_MANUAL_TOPUP_RUBLES * 100)
    values = {'minimum': _rubles(lower), 'maximum': _rubles(upper), 'currency': settings.CURRENCY_SYMBOL}
    if lower > upper:
        return texts.t(
            'TOPUP_RETRY_NO_RANGE_CURRENCY' if texts.language == 'fa' else 'TOPUP_RETRY_NO_RANGE',
            '❌ У этого способа нет допустимой суммы для ручного ввода '
            '(минимум {minimum} {currency}, максимум {maximum} {currency}). Выберите другой способ оплаты.',
        ).format(**values)
    key = 'TOPUP_RETRY_TOO_LOW_CURRENCY' if too_low else 'TOPUP_RETRY_TOO_HIGH_CURRENCY'
    if texts.language != 'fa':
        key = 'TOPUP_RETRY_TOO_LOW' if too_low else 'TOPUP_RETRY_TOO_HIGH'
    fallback = (
        '❌ Минимальная сумма пополнения — {minimum} {currency}.\n\n'
        if too_low
        else '❌ Максимальная сумма пополнения — {maximum} {currency}.\n\n'
    ) + (
        'Чтобы продолжить, отправьте боту сообщение с суммой пополнения не меньше {minimum} {currency} и не больше {maximum} {currency}.'
    )
    message = texts.t(key, fallback).format(**values)
    whole_ruble_example = (lower + 99) // 100
    if whole_ruble_example * 100 <= upper:
        message += '\n\n' + texts.t('TOPUP_RETRY_EXAMPLE', 'Например, отправьте: {amount}').format(
            amount=whole_ruble_example,
        )
    return message
