"""HooshPay client (hooshpay.xyz/api/v1): card-to-card invoices with instant verify.

Flow: create an invoice (amount in whole Toman), send the buyer to the hosted
``payment_url``, then receive an HMAC-signed webhook on success. Amounts at the
provider are whole Toman integers; our canonical unit is integer kopeks
(1/100 Toman), so every conversion goes through //100 and *100.

Auth: header ``X-API-KEY`` on every request. The webhook is signed separately
with the account Secret (HMAC-SHA256 over the key-sorted JSON body).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

_KOPEKS_IN_TOMAN = 100


class HooshPayAPIError(Exception):
    """HooshPay answered with an error status (400/401/403/404/503)."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f'HooshPay API error ({status_code}): {message}')


class HooshPayNetworkError(Exception):
    """No response received (connection drop or timeout). Outcome unknown."""


def toman_to_kopeks(value: Any) -> int | None:
    """Provider amount (whole Toman) -> integer kopeks, or None if unparsable.

    Accepts int or numeric string. Refuses fractional Toman: silently rounding
    money is worse than refusing the reconciliation.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        toman = int(str(value).strip())
    except (ValueError, TypeError):
        return None
    return toman * _KOPEKS_IN_TOMAN


def kopeks_to_toman(amount_kopeks: int) -> int:
    """Kopeks -> whole Toman for the request body (HooshPay wants integers)."""
    return int(amount_kopeks // _KOPEKS_IN_TOMAN)


class HooshPayService:
    """REST client for HooshPay v1 (hooshpay.xyz/api/v1)."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.HOOSHPAY_BASE_URL or 'https://hooshpay.xyz/api/v1').rstrip('/')

    @property
    def api_key(self) -> str:
        return settings.HOOSHPAY_API_KEY or ''

    @property
    def api_secret(self) -> str:
        return settings.HOOSHPAY_API_SECRET or ''

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _headers(self) -> dict[str, str]:
        return {
            'X-API-KEY': self.api_key,
            'Content-Type': 'application/json',
        }

    @staticmethod
    def _error_message(data: Any) -> str:
        if isinstance(data, dict):
            return str(data.get('message') or data.get('error') or data)
        return str(data)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        url = f'{self.base_url}/{path.lstrip("/")}'
        try:
            session = await self._get_session()
            async with session.request(method, url, json=json_payload, headers=self._headers()) as response:
                data = await response.json(content_type=None)

                if response.status == 404 and allow_404:
                    return None

                if response.status >= 400:
                    message = self._error_message(data)
                    logger.error('HooshPay API error', url=url, status=response.status, message=message)
                    raise HooshPayAPIError(response.status, message)

                return data if isinstance(data, dict) else {'_raw': data}
        except (aiohttp.ClientError, TimeoutError) as error:
            logger.error('HooshPay API connection error', url=url, error=str(error))
            raise HooshPayNetworkError(str(error)) from error

    @staticmethod
    def _unwrap(data: dict[str, Any] | None) -> dict[str, Any] | None:
        """HooshPay wraps results as {"success": true, "data": {...}}."""
        if not isinstance(data, dict):
            return None
        if data.get('success') is False:
            return None
        inner = data.get('data')
        return inner if isinstance(inner, dict) else data

    async def create_invoice(
        self,
        *,
        order_id: str,
        amount_kopeks: int,
        description: str | None = None,
        fee_mode: str | None = None,
        callback_url: str | None = None,
        return_url: str | None = None,
    ) -> dict[str, Any]:
        """POST /invoices — creates an invoice and returns its ``data`` block.

        ``fee_mode`` is only sent when explicitly configured; otherwise HooshPay
        applies the account default (per the provider's own setting).
        """
        payload: dict[str, Any] = {
            'amount': kopeks_to_toman(amount_kopeks),
            'order_id': order_id[:64],
        }
        if fee_mode:
            payload['fee_mode'] = fee_mode
        if description:
            payload['description'] = description[:255]
        if callback_url:
            payload['callback_url'] = callback_url
        if return_url:
            payload['return_url'] = return_url

        logger.info('HooshPay create_invoice', order_id=order_id, amount_kopeks=amount_kopeks)

        data = self._unwrap(await self._request('POST', '/invoices', json_payload=payload))
        if not data or not data.get('uid') or not data.get('payment_url'):
            logger.error('HooshPay create_invoice: incomplete response', order_id=order_id, response_data=data)
            raise HooshPayAPIError(200, f'Incomplete create invoice response: {data}')

        logger.info('HooshPay invoice created', order_id=order_id, uid=data.get('uid'), status=data.get('status'))
        return data

    async def get_invoice(self, *, uid: str) -> dict[str, Any] | None:
        """GET /invoices/{uid} — current invoice state."""
        if not uid:
            raise ValueError('HooshPay get_invoice: uid is required')
        return self._unwrap(await self._request('GET', f'/invoices/{uid}', allow_404=True))

    async def verify_invoice(self, *, uid: str) -> dict[str, Any] | None:
        """POST /invoices/{uid}/verify — forces a final payment check.

        Returns the full response ({success, paid, status, data}) so the caller
        can read the ``paid`` flag directly.
        """
        if not uid:
            raise ValueError('HooshPay verify_invoice: uid is required')
        return await self._request('POST', f'/invoices/{uid}/verify', allow_404=True)

    async def cancel_invoice(self, *, uid: str) -> dict[str, Any] | None:
        """POST /invoices/{uid}/cancel — only pending invoices can be cancelled."""
        if not uid:
            raise ValueError('HooshPay cancel_invoice: uid is required')
        return await self._request('POST', f'/invoices/{uid}/cancel', allow_404=True)

    # ------------------------------------------------------------------
    # Webhook signature
    # ------------------------------------------------------------------

    @staticmethod
    def parse_callback_body(raw_body: bytes) -> dict[str, Any] | None:
        """Parses the webhook JSON body into a dict (real int/str types kept).

        The signature is recomputed over the key-sorted re-serialization of this
        parsed body, so — unlike concatenation-based schemes — numbers must stay
        real ints here, not strings.
        """
        try:
            data = json.loads(raw_body)
        except (ValueError, TypeError) as error:
            logger.error('HooshPay callback: failed to parse JSON', error=str(error))
            return None
        if not isinstance(data, dict):
            logger.error('HooshPay callback: body is not a JSON object')
            return None
        return data

    @classmethod
    def build_signature_payload(cls, body: dict[str, Any]) -> str:
        """PHP side signs ``json_encode(ksort($payload), UNESCAPED_UNICODE|UNESCAPED_SLASHES)``.

        The Python equivalent: keys sorted, no spaces between separators, unicode
        left intact and slashes unescaped (both are Python's json defaults).
        """
        return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':'))

    def verify_callback_signature(self, body: dict[str, Any], signature: str | None) -> bool:
        """Verifies X-HooshPay-Signature — HMAC-SHA256 on the account Secret."""
        try:
            received = (signature or '').strip()
            if not received:
                logger.warning('HooshPay callback: missing X-HooshPay-Signature')
                return False

            secret = self.api_secret
            if not secret:
                # With an empty key the HMAC would be a known value — anyone
                # could forge the signature.
                logger.error('HooshPay callback: signature secret is not set, verification impossible')
                return False

            payload = self.build_signature_payload(body)
            expected = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()

            if not hmac.compare_digest(expected, received.lower()):
                logger.warning('HooshPay callback: invalid signature', received_prefix=received[:8])
                return False
            return True
        except Exception as error:
            logger.error('HooshPay callback verify error', error=str(error))
            return False


# Singleton instance
hooshpay_service = HooshPayService()
