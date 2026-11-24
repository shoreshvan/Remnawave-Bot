"""Translate recognized legacy descriptions at display time, without changing ledger rows."""
from app.localization.texts import get_texts


def localize_transaction_description(description: str | None, language: str, payment_method: str | None = None) -> str | None:
    if not description:
        return description
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
