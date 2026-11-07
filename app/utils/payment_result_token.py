import hashlib
import hmac

from app.config import settings


def payment_result_token(order_id: str) -> str:
    return hmac.new(
        settings.BOT_TOKEN.encode(),
        f'nowpayments-result:{order_id}'.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
