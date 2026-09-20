import html
import re
from datetime import UTC, datetime

from app.localization.texts import get_texts


# Формат Telegram-логина: 5-32 символа, первый — буква. Тот же шаблон используется
# в app/services/guest_purchase_service.py при приёме логина от пользователя.
_TELEGRAM_USERNAME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{4,31}$')


def format_datetime(dt: datetime | str, format_str: str = '%d.%m.%Y %H:%M') -> str:
    if isinstance(dt, str):
        if dt == 'now' or dt == '':
            dt = datetime.now(UTC)
        else:
            try:
                dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                dt = datetime.now(UTC)

    return dt.strftime(format_str)


def format_date(dt: datetime | str, format_str: str = '%d.%m.%Y') -> str:
    if isinstance(dt, str):
        if dt == 'now' or dt == '':
            dt = datetime.now(UTC)
        else:
            try:
                dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                dt = datetime.now(UTC)

    return dt.strftime(format_str)


def format_time_ago(dt: datetime | str, language: str = 'ru') -> str:
    if isinstance(dt, str):
        if dt == 'now' or dt == '':
            dt = datetime.now(UTC)
        else:
            try:
                dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                dt = datetime.now(UTC)

    now = datetime.now(UTC)
    diff = now - dt

    language_code = (language or 'ru').split('-')[0].lower()
    texts = get_texts(language_code)

    if diff.days > 0:
        if diff.days == 1:
            return texts.t('TIME_AGO_YESTERDAY', 'yesterday' if language_code == 'en' else 'вчера')
        if diff.days < 7:
            value = diff.days
            if language_code == 'en':
                default = '{value} day ago' if value == 1 else '{value} days ago'
            else:
                default = '{value} дн. назад'
            return texts.t('TIME_AGO_DAYS', default).format(value=value)
        if diff.days < 30:
            value = diff.days // 7
            if language_code == 'en':
                default = '{value} week ago' if value == 1 else '{value} weeks ago'
            else:
                default = '{value} нед. назад'
            return texts.t('TIME_AGO_WEEKS', default).format(value=value)
        if diff.days < 365:
            value = diff.days // 30
            if language_code == 'en':
                default = '{value} month ago' if value == 1 else '{value} months ago'
            else:
                default = '{value} мес. назад'
            return texts.t('TIME_AGO_MONTHS', default).format(value=value)
        value = diff.days // 365
        if language_code == 'en':
            default = '{value} year ago' if value == 1 else '{value} years ago'
        else:
            default = '{value} г. назад'
        return texts.t('TIME_AGO_YEARS', default).format(value=value)

    if diff.seconds > 3600:
        value = diff.seconds // 3600
        if language_code == 'en':
            default = '{value} hour ago' if value == 1 else '{value} hours ago'
        else:
            default = '{value} ч. назад'
        return texts.t('TIME_AGO_HOURS', default).format(value=value)

    if diff.seconds > 60:
        value = diff.seconds // 60
        if language_code == 'en':
            default = '{value} minute ago' if value == 1 else '{value} minutes ago'
        else:
            default = '{value} мин. назад'
        return texts.t('TIME_AGO_MINUTES', default).format(value=value)

    return texts.t('TIME_AGO_JUST_NOW', 'just now' if language_code == 'en' else 'только что')


def format_days_declension(days: int, language: str = 'ru') -> str:
    language_code = (language or 'ru').split('-')[0].lower()
    texts = get_texts(language_code)
    if language_code not in {'ru', 'fa'}:
        default = '{days} day' if days == 1 else '{days} days'
        return texts.t('DAYS_DECLENSION_OTHER', default).format(days=days)

    if days % 10 == 1 and days % 100 != 11:
        return texts.t('DAYS_DECLENSION_ONE', '{days} день').format(days=days)
    if days % 10 in [2, 3, 4] and days % 100 not in [12, 13, 14]:
        return texts.t('DAYS_DECLENSION_FEW', '{days} дня').format(days=days)
    return texts.t('DAYS_DECLENSION_MANY', '{days} дней').format(days=days)


def format_duration(seconds: int) -> str:
    texts = get_texts()

    if seconds < 60:
        return texts.t('DURATION_SECONDS', '{seconds} сек.').format(seconds=seconds)

    minutes = seconds // 60
    if minutes < 60:
        return texts.t('DURATION_MINUTES', '{minutes} мин.').format(minutes=minutes)

    hours = minutes // 60
    if hours < 24:
        return texts.t('DURATION_HOURS', '{hours} ч.').format(hours=hours)

    days = hours // 24
    return texts.t('DURATION_DAYS', '{days} дн.').format(days=days)


def format_bytes(bytes_value: int) -> str:
    if bytes_value == 0:
        return '0 B'

    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = float(bytes_value)
    unit_index = 0

    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1

    if size == int(size):
        return f'{int(size)} {units[unit_index]}'
    return f'{size:.1f} {units[unit_index]}'


def format_percentage(value: float, decimals: int = 1) -> str:
    return f'{value:.{decimals}f}%'


def format_number(number: float, separator: str = ' ') -> str:
    if isinstance(number, float):
        integer_part = int(number)
        decimal_part = number - integer_part

        formatted_integer = f'{integer_part:,}'.replace(',', separator)

        if decimal_part > 0:
            return f'{formatted_integer}.{decimal_part:.2f}'.split('.')[0] + f'.{str(decimal_part).split(".")[1][:2]}'
        return formatted_integer
    return f'{number:,}'.replace(',', separator)


def format_price_range(min_price: int, max_price: int) -> str:
    from app.config import settings

    min_formatted = settings.format_price(min_price)
    max_formatted = settings.format_price(max_price)

    if min_price == max_price:
        return min_formatted
    return f'{min_formatted} - {max_formatted}'


def truncate_text(text: str, max_length: int = 100, suffix: str = '...') -> str:
    if len(text) <= max_length:
        return text

    return text[: max_length - len(suffix)] + suffix


def format_username(username: str | None, user_id: int, full_name: str | None = None) -> str:
    if full_name:
        return full_name
    if username:
        return f'@{username}'
    return f'ID{user_id}'


def format_username_link(username: str | None, fallback: str = '') -> str:
    """Telegram-логин явной ссылкой — для rich-сообщений.

    Rich-сообщения уходят со skip_entity_detection=True (app/utils/rich_admin.py,
    app/utils/rich_menu.py), поэтому голый @username в них не подсвечивается:
    ссылку приходится ставить руками.

    Ссылка ставится только на то, что выглядит настоящим Telegram-логином.
    Колонка users.username хранит не только их: OAuth-регистрация в кабинете кладёт
    туда логин Discord/Яндекса (app/cabinet/auth/oauth_providers.py), а это чужое
    пространство имён — t.me/<логин> оттуда ведёт либо в никуда, либо на
    постороннего человека с таким же ником. Остальное отдаём текстом, как было.
    """
    if not username:
        return fallback

    normalized_username = username.lstrip('@')
    if not normalized_username:
        return fallback

    safe_username = html.escape(normalized_username, quote=True)
    if not _TELEGRAM_USERNAME_RE.match(normalized_username):
        return f'@{safe_username}'
    return f'<a href="https://t.me/{safe_username}">@{safe_username}</a>'


def format_subscription_status(is_active: bool, is_trial: bool, end_date: datetime | str, language: str = 'ru') -> str:
    if isinstance(end_date, str):
        try:
            end_date = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            end_date = datetime.now(UTC)

    language_code = (language or 'ru').split('-')[0].lower()
    use_russian_fallback = language_code in {'ru', 'fa'}
    texts = get_texts(language_code)

    if not is_active:
        return texts.t('ADMIN_USER_SUBSCRIPTION_STATUS_INACTIVE', '❌ Неактивна')

    if is_trial:
        status = texts.t('MAIN_MENU_RICH_STATUS_TRIAL', '🎁 Тестовая')
    else:
        status = texts.t('ADMIN_USER_SUBSCRIPTION_STATUS_ACTIVE', '✅ Активна')

    now = datetime.now(UTC)
    if end_date > now:
        days_left = (end_date - now).days
        if days_left > 0:
            default = ' ({days} дн.)' if use_russian_fallback else ' ({days} days)'
            status += texts.t('SUBSCRIPTION_STATUS_DAYS_LEFT', default).format(days=days_left)
        else:
            hours_left = (end_date - now).seconds // 3600
            default = ' ({hours} ч.)' if use_russian_fallback else ' ({hours} hrs)'
            status += texts.t('SUBSCRIPTION_STATUS_HOURS_LEFT', default).format(hours=hours_left)
    else:
        status = texts.t('SUBSCRIPTION_STATUS_EXPIRED_LABEL', '⏰ Истекла' if use_russian_fallback else '⏰ Expired')

    return status


def format_traffic_usage(used_gb: float, limit_gb: int, language: str = 'ru') -> str:
    language_code = (language or 'ru').split('-')[0].lower()
    use_russian_fallback = language_code in {'ru', 'fa'}
    texts = get_texts(language_code)
    used = f'{used_gb:.1f}'

    if limit_gb == 0:
        default = '{used} ГБ / ∞' if use_russian_fallback else '{used} GB / ∞'
        return texts.t('TRAFFIC_USAGE_UNLIMITED', default).format(used=used)

    percentage = (used_gb / limit_gb) * 100 if limit_gb > 0 else 0

    if use_russian_fallback:
        default = '{used} ГБ / {limit} ГБ ({percent}%)'
    else:
        default = '{used} GB / {limit} GB ({percent}%)'
    return texts.t('TRAFFIC_USAGE_LIMITED', default).format(used=used, limit=limit_gb, percent=f'{percentage:.1f}')


def format_boolean(value: bool, language: str = 'ru') -> str:
    language_code = (language or 'ru').split('-')[0].lower()
    texts = get_texts(language_code)
    if value:
        return texts.t('YES', '✅ Да')
    return texts.t('NO', '❌ Нет')
