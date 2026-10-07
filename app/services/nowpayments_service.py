"""NOWPayments client (api.nowpayments.io/v1): crypto intake via hosted invoice.

Flow: create an invoice priced in USD (provider knows no IRR — Toman is
converted with the manual rate), send the buyer to ``invoice_url`` where they
pick the pay currency, then learn the outcome via IPN and/or ``GET payment``
polling. Our canonical unit is integer kopeks (1/100 Toman).

Auth: header ``x-api-key`` on every request. IPN authenticity:
``x-nowpayments-sig`` = HMAC-SHA512 over the key-sorted compact JSON body
with the account IPN Secret (exact docs recipe — sorted keys, compact
separators). A raw-bytes fallback covers proxies that reformat spacing.

Credit rule: ONLY ``finished`` credits. ``confirmed`` means the chain saw it
but funds are not ours yet; ``partially_paid`` is an accepted underpayment
and must go to manual review, never auto-credit.
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


class NowPaymentsAPIError(Exception):
    """NOWPayments answered with an error status (400/401/404/503)."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f'NOWPayments API error ({status_code}): {message}')


class NowPaymentsNetworkError(Exception):
    """No response received (connection drop or timeout). Outcome unknown."""


class NowPaymentsService:
    """REST client for NOWPayments v1 (invoice flow)."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.NOWPAYMENTS_BASE_URL or 'https://api.nowpayments.io/v1').rstrip('/')

    @property
    def api_key(self) -> str:
        return settings.NOWPAYMENTS_API_KEY or ''

    @property
    def ipn_secret(self) -> str:
        return settings.NOWPAYMENTS_IPN_SECRET or ''

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
            'x-api-key': self.api_key,
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
                try:
                    data = await response.json(content_type=None)
                except (ValueError, TypeError, aiohttp.ContentTypeError) as error:
                    # Non-JSON or empty body (e.g. upstream 502 page).
                    if response.status == 404 and allow_404:
                        return None
                    message = f'Non-JSON response (HTTP {response.status}): {error}'
                    logger.error('NOWPayments API error', url=url, status=response.status, message=message)
                    raise NowPaymentsAPIError(response.status, message) from error

                if response.status == 404 and allow_404:
                    return None

                if response.status >= 400:
                    message = self._error_message(data)
                    logger.error('NOWPayments API error', url=url, status=response.status, message=message)
                    raise NowPaymentsAPIError(response.status, message)

                return data if isinstance(data, dict) else {'_raw': data}
        except (NowPaymentsAPIError, NowPaymentsNetworkError):
            raise
        except (aiohttp.ClientError, TimeoutError) as error:
            logger.error('NOWPayments API connection error', url=url, error=str(error))
            raise NowPaymentsNetworkError(str(error)) from error

    async def create_invoice(
        self,
        *,
        order_id: str,
        price_usd: float,
        order_description: str | None = None,
        ipn_callback_url: str | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
        partially_paid_url: str | None = None,
    ) -> dict[str, Any]:
        """POST /invoice — hosted pay page. Buyer picks the pay currency there."""
        payload: dict[str, Any] = {
            'price_amount': price_usd,
            'price_currency': 'usd',
            'order_id': order_id[:64],
        }
        if order_description:
            payload['order_description'] = order_description[:255]
        if ipn_callback_url:
            payload['ipn_callback_url'] = ipn_callback_url
        if success_url:
            payload['success_url'] = success_url
        if cancel_url:
            payload['cancel_url'] = cancel_url
        if partially_paid_url:
            payload['partially_paid_url'] = partially_paid_url

        logger.info('NOWPayments create_invoice', order_id=order_id, price_usd=price_usd)

        data = await self._request('POST', '/invoice', json_payload=payload)
        if not data or data.get('id') is None or not data.get('invoice_url'):
            logger.error('NOWPayments create_invoice: incomplete response', order_id=order_id, response_data=data)
            raise NowPaymentsAPIError(201, f'Incomplete create invoice response: {self._error_message(data)}')

        logger.info(
            'NOWPayments invoice created',
            order_id=order_id,
            invoice_id=data.get('id'),
        )
        return data

    async def get_payment_status(self, *, payment_id: str | int) -> dict[str, Any] | None:
        """GET /payment/{id} — current payment state (authoritative for credit)."""
        if payment_id is None or str(payment_id) == '':
            raise ValueError('NOWPayments get_payment_status: payment_id is required')
        return await self._request('GET', f'/payment/{payment_id}', allow_404=True)

    # ------------------------------------------------------------------
    # IPN signature (docs recipe, byte-exact)
    # ------------------------------------------------------------------

    @staticmethod
    def parse_callback_body(raw_body: bytes) -> dict[str, Any] | None:
        """Parses the IPN JSON body into a dict."""
        try:
            data = json.loads(raw_body)
        except (ValueError, TypeError) as error:
            logger.error('NOWPayments callback: failed to parse JSON', error=str(error))
            return None
        if not isinstance(data, dict):
            logger.error('NOWPayments callback: body is not a JSON object')
            return None
        return data

    @staticmethod
    def build_signature_payload(body: dict[str, Any]) -> str:
        """Docs recipe: key-sorted compact JSON (their own Python sample)."""
        return json.dumps(body, sort_keys=True, separators=(',', ':'))

    def verify_callback_signature(
        self,
        raw_body: bytes,
        parsed: dict[str, Any],
        signature: str | None,
    ) -> bool:
        """Verifies x-nowpayments-sig — HMAC-SHA512 with the IPN Secret."""
        try:
            received = (signature or '').strip().lower()
            if not received:
                logger.warning('NOWPayments callback: missing x-nowpayments-sig')
                return False
            if not self.ipn_secret:
                logger.error('NOWPayments callback: IPN secret is not set, verification impossible')
                return False

            candidates = [raw_body, self.build_signature_payload(parsed).encode('utf-8')]
            for candidate in candidates:
                expected = hmac.new(self.ipn_secret.encode('utf-8'), candidate, hashlib.sha512).hexdigest()
                if hmac.compare_digest(expected, received):
                    return True
            logger.warning('NOWPayments callback: invalid signature')
            return False
        except Exception as error:
            logger.error('NOWPayments callback verify error', error=str(error))
            return False


# Singleton instance
nowpayments_service = NowPaymentsService()
