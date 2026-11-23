"""Translate recognized legacy descriptions at display time, without changing ledger rows."""
from app.localization.texts import get_texts


def localize_transaction_description(description: str | None, language: str) -> str | None:
    if not description:
        return description
    prefix = 'Покупка суточного тарифа '
    suffix = ' (первый день)'
    if description.startswith(prefix) and description.endswith(suffix):
        tariff = description[len(prefix):-len(suffix)]
        return get_texts(language).t(
            'TRANSACTION_DAILY_PLAN_PURCHASE',
            'Покупка суточного тарифа {tariff} (первый день)',
        ).format(tariff=tariff)
    return description
