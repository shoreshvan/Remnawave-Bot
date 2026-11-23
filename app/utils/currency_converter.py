import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class CurrencyConverter:
    """USD ↔ home-currency (Toman) conversion from an operator-set rate.

    The class/method names keep the historical ``rub`` wording purely for
    back-compat with the call sites that import ``currency_converter`` —
    here ``rub`` means home-currency units (Toman), never rubles.

    Iran's USD rate is not published the way USD/RUB was, so the old live
    fetch (CBR / exchangerate-api / fixer), its ``50 < rate < 200`` sanity
    band and the ``95.0`` fallback are all gone. The rate now comes from
    ``settings.CRYPTOBOT_USD_TO_TOMAN_RATE``, read on every call so an
    admin-panel edit (which setattr-s the live settings object) or an
    ``.env`` value takes effect with no code change and no restart.
    """

    def _get_rate(self) -> float:
        raw = getattr(settings, 'CRYPTOBOT_USD_TO_TOMAN_RATE', 0.0)
        try:
            rate = float(raw)
        except (TypeError, ValueError):
            rate = 0.0

        if rate <= 0:
            fallback = float(type(settings).model_fields['CRYPTOBOT_USD_TO_TOMAN_RATE'].default)
            logger.warning(
                'CRYPTOBOT_USD_TO_TOMAN_RATE is not positive, falling back to default',
                configured=raw,
                fallback=fallback,
            )
            return fallback
        return rate

    async def get_usd_to_rub_rate(self) -> float:
        """Toman per 1 USD (operator-set; ``rub`` == home currency)."""
        return self._get_rate()

    async def usd_to_rub(self, usd_amount: float) -> float:
        """Convert a USD amount to the home currency (Toman)."""
        return usd_amount * self._get_rate()

    async def rub_to_usd(self, rub_amount: float) -> float:
        """Convert a home-currency (Toman) amount to USD."""
        return rub_amount / self._get_rate()


# Глобальный экземпляр
currency_converter = CurrencyConverter()
