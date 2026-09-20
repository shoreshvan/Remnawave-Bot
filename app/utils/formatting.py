"""Shared formatting utilities for traffic, price, and period display."""

import html

from app.localization.texts import get_texts


def safe_html_name(name: str | None) -> str:
    """HTML-escape a display name for Telegram HTML messages."""
    return html.escape(name or '')


def user_html_link(user) -> str:
    """Build an HTML-safe clickable user link for Telegram messages."""
    safe = safe_html_name(user.full_name)
    if getattr(user, 'telegram_id', None):
        return f'<a href="tg://user?id={user.telegram_id}">{safe}</a>'
    return f'<b>{safe}</b>'


def format_traffic(gb: int) -> str:
    """Форматирует трафик."""
    texts = get_texts()
    if gb == 0:
        return texts.t('TRAFFIC_UNLIMITED_SHORT', 'Безлимит')
    return texts.t('TARIFF_PURCHASE_TRAFFIC_GB', '{traffic} ГБ').format(traffic=gb)


def format_price_kopeks(kopeks: int, compact: bool = False) -> str:
    """Форматирует цену из копеек в рубли."""
    rubles = kopeks / 100
    if compact:
        # Компактный формат - округляем до рублей
        return f'{int(round(rubles))}₽'
    if rubles == int(rubles):
        return f'{int(rubles)} ₽'
    return f'{rubles:.2f} ₽'


def format_period(days: int) -> str:
    """Форматирует период."""
    texts = get_texts()
    mod100 = days % 100
    mod10 = days % 10
    if 11 <= mod100 <= 19:
        key, default = 'DAYS_DECLENSION_MANY', '{days} дней'
    elif mod10 == 1:
        key, default = 'DAYS_DECLENSION_ONE', '{days} день'
    elif 2 <= mod10 <= 4:
        key, default = 'DAYS_DECLENSION_FEW', '{days} дня'
    else:
        key, default = 'DAYS_DECLENSION_MANY', '{days} дней'
    return texts.t(key, default).format(days=days)
