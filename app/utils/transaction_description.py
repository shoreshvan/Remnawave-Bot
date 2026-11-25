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
        charge = re.fullmatch(
            r"(?:(Покупка|Продление) тарифа (.+) на ([0-9]+) дней|Активация суточного тарифа '(.+)')"
            r"(?: \(скидка ([0-9]+(?:\.[0-9]+)?)%\))?(?: \(промо -([0-9]+(?:\.[0-9]+)?)%\))?",
            description, re.DOTALL,
        )
        if charge:
            operation, tariff, days, daily_tariff, discount, promo = charge.groups()
            texts = get_texts('fa')
            if daily_tariff is not None:
                result = texts.t('AUTO_PURCHASE_DAILY_ACTIVATION_DESCRIPTION').format(tariff=daily_tariff)
            else:
                if tariff.startswith("'") and tariff.endswith("'") or tariff.startswith('«') and tariff.endswith('»'):
                    tariff = tariff[1:-1]
                key = 'TARIFF_PURCHASE_TRANSACTION_DESCRIPTION' if operation == 'Покупка' else 'AUTO_RENEWAL_TARIFF_DESCRIPTION'
                result = texts.t(key).format(tariff=tariff, days=days)
            if discount:
                result += texts.t('TARIFF_PURCHASE_DISCOUNT_SUFFIX').format(percent=discount)
            if promo:
                result += texts.t('TARIFF_PURCHASE_PROMO_SUFFIX').format(percent=promo)
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
