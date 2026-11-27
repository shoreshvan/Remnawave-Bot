"""Translate recognized legacy descriptions at display time, without changing ledger rows."""
import re

from app.localization.texts import get_texts


def localize_transaction_description(description: str | None, language: str, payment_method: str | None = None) -> str | None:
    if not description:
        return description
    switch = re.fullmatch(r"Смена тарифа администратором на '(.*)'", description, re.DOTALL)
    if switch:
        return get_texts(language or 'fa').t(
            'TARIFF_SWITCH_ADMIN_TRANSACTION_DESCRIPTION', "Смена тарифа администратором на '{tariff}'"
        ).format(tariff=switch[1])
    purchase = re.fullmatch(r'Покупка тарифа (.*) на ([0-9]+) дней \(администратор\)', description, re.DOTALL)
    if purchase:
        return get_texts(language or 'fa').t(
            'TARIFF_PURCHASE_ADMIN_TRANSACTION_DESCRIPTION', 'Покупка тарифа {tariff} на {days} дней (администратор)'
        ).format(tariff=purchase[1], days=purchase[2])
    if (language or 'fa') == 'fa':
        traffic = re.fullmatch(r'Переключение трафика с (\d+)GB на (\d+)GB(?: за (\d+) дн\.)?', description)
        if traffic:
            result = get_texts('fa').t('MINIAPP_TRANSACTION_TRAFFIC_SWITCH').format(old=traffic[1], new=traffic[2])
            if traffic[3]:
                return get_texts('fa').t('MINIAPP_TRANSACTION_DESCRIPTION_FOR_DAYS').format(description=result, days=traffic[3])
            return result
        devices = re.fullmatch(r'Изменение количества устройств с (\d+) до (\d+)(?: за (\d+) дн\.)?', description)
        if devices:
            result = get_texts('fa').t('MINIAPP_TRANSACTION_DEVICE_LIMIT_CHANGE').format(old=devices[1], new=devices[2])
            if devices[3]:
                return get_texts('fa').t('MINIAPP_TRANSACTION_DESCRIPTION_FOR_DAYS').format(description=result, days=devices[3])
            return result
        legacy_gift = re.fullmatch(r'Gift: (.+) \(([0-9]+)d\)(?: -> (.*))?', description, re.DOTALL)
        if legacy_gift:
            result = get_texts('fa').t('CABINET_GIFT_PAYMENT_DESCRIPTION').format(
                tariff_name=legacy_gift[1], period_days=legacy_gift[2]
            )
            return result + (f' -> {legacy_gift[3]}' if legacy_gift[3] else '')
        legacy_receipts = (
            (r'Добавление серверов: (?P<servers>.+) за (?P<days>[0-9]+) дн\.', 'MINIAPP_TRANSACTION_SERVERS_ADDED'),
            (r'Изменение списка серверов', 'MINIAPP_TRANSACTION_SERVERS_CHANGED'),
            (r'Бонус по промокоду (?P<code>.+)', 'PROMOCODE_BALANCE_LEDGER'),
            (r"Активация суточного тарифа '(?P<tariff>.*)' \(первый день\)", 'MINIAPP_TRANSACTION_DAILY_TARIFF_ACTIVATION'),
            (r"Переход с суточного на тариф '(?P<tariff>.*)' \((?P<days>[0-9]+) дней\)", 'MINIAPP_TRANSACTION_SWITCH_FROM_DAILY'),
            (r"Переход на тариф '(?P<tariff>.*)' \(доплата за (?P<days>[0-9]+) дней\)", 'MINIAPP_TRANSACTION_SWITCH_TO_TARIFF_PRORATED'),
            (r"Переход на тариф '(?P<tariff>.*)'", 'MINIAPP_TRANSACTION_SWITCH_TO_TARIFF'),
            (r'Докупка (?P<gb>[0-9]+) ГБ трафика', 'MINIAPP_TRANSACTION_TRAFFIC_TOPUP'),
            (r'Докупка (?P<gb>[0-9]+) ГБ трафика \(скидка (?P<percent>[0-9]+(?:\.[0-9]+)?)%\)', 'MINIAPP_TRANSACTION_TRAFFIC_TOPUP_DISCOUNT'),
            (r'Суточная оплата тарифа «(?P<tariff_name>.*)»', 'DAILY_CHARGE_TRANSACTION_DESCRIPTION'),
            (r'Суточная оплата тарифа «(?P<tariff_name>.*)» \(возобновление\)', 'CABINET_DAILY_RESUME_TRANSACTION_DESCRIPTION'),
            (r'Суточная оплата тарифа «(?P<tariff_name>.*)» \(авто-возобновление\)', 'DAILY_AUTO_RESUME_TRANSACTION_DESCRIPTION'),
            (r'Продление подписки на (?P<days>[0-9]+) дней', 'CABINET_RENEWAL_TRANSACTION_DESCRIPTION'),
            (r'Смена тарифа на (?P<tariff>.+) \((?P<days>[0-9]+) дней\)', 'TARIFF_SWITCH_RECEIPT_PERIOD'),
            (r'Переключение на суточный тариф (?P<tariff>.+) \(первый день\)', 'TARIFF_SWITCH_RECEIPT_DAILY'),
            (r'(?:Смена тарифа на|Переключение на тариф) (?P<tariff>.+)', 'TARIFF_SWITCH_RECEIPT'),
        )
        for pattern, key in legacy_receipts:
            match = re.fullmatch(pattern, description, re.DOTALL)
            if match:
                return get_texts('fa').t(key).format(**match.groupdict())
        charge = re.fullmatch(
            r"(?:(Покупка|Продление) тарифа (.+) на ([0-9]+) дней|Активация суточного тарифа '(.+)')"
            r"(?: \(скидка ([0-9]+(?:\.[0-9]+)?)%\))?(?: \(промо -([0-9]+(?:\.[0-9]+)?)%\))?",
            description, re.DOTALL,
        )
        if charge:
            operation, tariff, days, daily_tariff, discount, promo = charge.groups()
            if daily_tariff is not None:
                result = get_texts('fa').t('AUTO_PURCHASE_DAILY_ACTIVATION_DESCRIPTION').format(tariff=daily_tariff)
            else:
                if tariff.startswith("'") and tariff.endswith("'") or tariff.startswith('«') and tariff.endswith('»'):
                    tariff = tariff[1:-1]
                key = 'TARIFF_PURCHASE_TRANSACTION_DESCRIPTION' if operation == 'Покупка' else 'AUTO_RENEWAL_TARIFF_DESCRIPTION'
                result = get_texts('fa').t(key).format(tariff=tariff, days=days)
            if discount:
                result += get_texts('fa').t('TARIFF_PURCHASE_DISCOUNT_SUFFIX').format(percent=discount)
            if promo:
                result += get_texts('fa').t('TARIFF_PURCHASE_PROMO_SUFFIX').format(percent=promo)
            return result
    if payment_method in {'atlaspay', 'hooshpay', 'nowpayments', 'tonpays'}:
        prefix = 'Пополнение через '
        if description.startswith(prefix) and description[len(prefix):]:
            return get_texts(language or 'fa').t(
                'NOWPAYMENTS_TRANSACTION_DESCRIPTION', 'Пополнение через {display_name}'
            ).format(display_name=description[len(prefix):])
    prefix = 'Покупка суточного тарифа '
    suffix = ' (первый день)'
    if description.startswith(prefix) and description.endswith(suffix):
        tariff = description[len(prefix):-len(suffix)]
        return get_texts(language or 'fa').t(
            'TRANSACTION_DAILY_PLAN_PURCHASE',
            'Покупка суточного тарифа {tariff} (первый день)',
        ).format(tariff=tariff)
    return description
