"""TonPays custom gateway client (tonpays.online, Telegram platform, card-to-card).

Flow: create an invoice (whole Toman + buyer Telegram id), show the buyer
``card_number``/``card_name`` + ``final_amount`` (request + unique delta),
optionally upload their receipt photo, then receive a webhook on completion.
Our canonical unit is integer kopeks (1/100 Toman); every conversion
goes through //100 and *100.

Auth: header ``X-API-Key`` on every request AND on the webhook delivery.
The webhook additionally carries ``X-TonPays-Signature`` /
``X-TonPays-Delivery-Id`` — but the docs publish no signature construction
scheme, so authenticity is established by comparing ``X-API-Key`` with the
configured key (constant-time) plus ``delivery_id`` idempotency. If TonPays
publishes the HMAC scheme, extend ``verify_callback_auth`` accordingly.
"""

from __future__ import annotations

import hmac
import json
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

_KOPEKS_IN_TOMAN = 100

#: Webhook event names that confirm money (may grow — unknown events stay pending).
TONPAYS_PAID_EVENTS = frozenset({'invoice.completed'})


class TonPaysAPIError(Exception):
    """TonPays answered with an error status (4xx/5xx) or a known error code."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f'TonPays API error ({status_code}): {message}')


class TonPaysNetworkError(Exception):
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
    """Kopeks -> whole Toman for the request body (TonPays wants integers)."""
    return int(amount_kopeks // _KOPEKS_IN_TOMAN)


class TonPaysService:
    """REST client for the TonPays custom Telegram platform."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.TONPAYS_BASE_URL or 'https://tonpays.online').rstrip('/')

    @property
    def api_key(self) -> str:
        return settings.TONPAYS_API_KEY or ''

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
            'X-API-Key': self.api_key,
            'Content-Type': 'application/json',
        }

    @staticmethod
    def _error_message(data: Any) -> str:
        if isinstance(data, dict):
            return str(data.get('message') or data.get('code') or data.get('error') or data)
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
                    logger.error('TonPays API error', url=url, status=response.status, message=message)
                    raise TonPaysAPIError(response.status, message) from error

                if response.status == 404 and allow_404:
                    return None

                if response.status >= 400:
                    message = self._error_message(data)
                    logger.error('TonPays API error', url=url, status=response.status, message=message)
                    raise TonPaysAPIError(response.status, message)

                return data if isinstance(data, dict) else {'_raw': data}
        except (TonPaysAPIError, TonPaysNetworkError):
            raise
        except (aiohttp.ClientError, TimeoutError) as error:
            logger.error('TonPays API connection error', url=url, error=str(error))
            raise TonPaysNetworkError(str(error)) from error

    async def create_invoice(
        self,
        *,
        order_id: str,
        amount_kopeks: int,
        buyer_chat_id: int,
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/custom/v1/invoices/telegram/create (HTTP 201)."""
        payload: dict[str, Any] = {
            'amount': kopeks_to_toman(amount_kopeks),
            'order_id': order_id[:20],
            'buyer_chat_id': int(buyer_chat_id),
        }
        if callback_url:
            payload['callback_url'] = callback_url

        logger.info('TonPays create_invoice', order_id=order_id, amount_kopeks=amount_kopeks)

        data = await self._request('POST', '/api/custom/v1/invoices/telegram/create', json_payload=payload)
        if not data or not data.get('invoice_id'):
            logger.error('TonPays create_invoice: incomplete response', order_id=order_id, response_data=data)
            raise TonPaysAPIError(201, f'Incomplete create invoice response: {self._error_message(data)}')

        logger.info(
            'TonPays invoice created',
            order_id=order_id,
            invoice_id=data.get('invoice_id'),
            status=data.get('status'),
        )
        return data

    async def check_invoice(self, *, invoice_id: str) -> dict[str, Any] | None:
        """GET /api/custom/v1/invoices/check/{invoice_id} — current state + paid flag."""
        if not invoice_id:
            raise ValueError('TonPays check_invoice: invoice_id is required')
        return await self._request('GET', f'/api/custom/v1/invoices/check/{invoice_id}', allow_404=True)

    async def change_card(self, *, invoice_id: str) -> dict[str, Any] | None:
        """POST /api/custom/v1/invoices/{invoice_id}/change-card — new card.

        Provider enforces a 60s cooldown, drops used cards and reports
        exhaustion (``change_card_exhausted``).
        """
        if not invoice_id:
            raise ValueError('TonPays change_card: invoice_id is required')
        return await self._request('POST', f'/api/custom/v1/invoices/{invoice_id}/change-card', allow_404=True)

    async def upload_receipt(
        self,
        *,
        invoice_id: str,
        file_bytes: bytes,
        filename: str = 'receipt.jpg',
    ) -> dict[str, Any] | None:
        """POST multipart /api/custom/v1/invoices/{invoice_id}/receipt (max 10 MB)."""
        if not invoice_id:
            raise ValueError('TonPays upload_receipt: invoice_id is required')
        if not file_bytes:
            raise ValueError('TonPays upload_receipt: empty file')
        if len(file_bytes) > settings.TONPAYS_RECEIPT_MAX_BYTES:
            raise TonPaysAPIError(413, f'Receipt exceeds {settings.TONPAYS_RECEIPT_MAX_BYTES} bytes')

        url = f'{self.base_url}/api/custom/v1/invoices/{invoice_id}/receipt'
        try:
            session = await self._get_session()
            form = aiohttp.FormData()
            form.add_field('file', file_bytes, filename=filename, content_type='image/jpeg')
            async with session.post(
                url, data=form, headers={'X-API-Key': self.api_key}
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except (ValueError, TypeError, aiohttp.ContentTypeError) as error:
                    message = f'Non-JSON response (HTTP {response.status}): {error}'
                    logger.error('TonPays API error', url=url, status=response.status, message=message)
                    raise TonPaysAPIError(response.status, message) from error
                if response.status >= 400:
                    message = self._error_message(data)
                    logger.error('TonPays receipt upload error', url=url, status=response.status, message=message)
                    raise TonPaysAPIError(response.status, message)
                logger.info('TonPays receipt uploaded', invoice_id=invoice_id, status=data.get('status'))
                return data if isinstance(data, dict) else {'_raw': data}
        except (TonPaysAPIError, TonPaysNetworkError):
            raise
        except (aiohttp.ClientError, TimeoutError) as error:
            logger.error('TonPays API connection error', url=url, error=str(error))
            raise TonPaysNetworkError(str(error)) from error

    # ------------------------------------------------------------------
    # Webhook auth
    # ------------------------------------------------------------------

    @staticmethod
    def parse_callback_body(raw_body: bytes) -> dict[str, Any] | None:
        """Parses the webhook JSON body into a dict."""
        try:
            data = json.loads(raw_body)
        except (ValueError, TypeError) as error:
            logger.error('TonPays callback: failed to parse JSON', error=str(error))
            return None
        if not isinstance(data, dict):
            logger.error('TonPays callback: body is not a JSON object')
            return None
        return data

    def verify_callback_auth(
        self,
        headers: dict[str, str] | Any,
        body: dict[str, Any],
    ) -> bool:
        """Verifies a webhook delivery.

        The docs publish no ``X-TonPays-Signature`` construction scheme, so the
        primary check is the ``X-API-Key`` header against the configured custom
        TG key (constant-time). ``delivery_id`` dedup happens in the mixin.
        """
        get = headers.get if hasattr(headers, 'get') else (lambda k, d=None: None)
        received_key = (get('X-API-Key') or get('x-api-key') or '').strip()
        if not received_key:
            logger.warning('TonPays callback: missing X-API-Key')
            return False
        if not self.api_key:
            logger.error('TonPays callback: API key is not set, verification impossible')
            return False
        if not hmac.compare_digest(received_key, self.api_key):
            logger.warning('TonPays callback: invalid X-API-Key')
            return False
        if not get('X-TonPays-Signature'):
            # Scheme undocumented — accept on API-key match, note for follow-up.
            logger.warning('TonPays callback: X-TonPays-Signature absent (scheme undocumented)')
        if not (body.get('delivery_id') or body.get('invoice_id')):
            logger.warning('TonPays callback: no delivery_id/invoice_id, cannot dedupe')
            return False
        return True


# Singleton instance
tonpays_service = TonPaysService()
