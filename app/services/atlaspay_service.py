"""AtlasPay client (api.atlaspay.space/api/v1): card-to-card with SMS auto-verify.

Flow: create an order (base Toman + optional customer Telegram id), send the
buyer to ``customerStartLink`` (Telegram mini-app showing the exact
``totalAmountToman``), then learn the outcome via webhook, ``verify`` (has the
authoritative ``paid`` flag) or polling ``GET``. Our canonical unit is integer
kopeks (1/100 Toman); every conversion goes through //100 and *100.

Critical trap (docs §4.1): an accepted underpayment ALSO yields
``paid: true`` with ``requiresManualDelivery: true`` — credit only when
``requiresManualDelivery`` is false. The webhook body carries NEITHER field,
so every webhook must be followed by a ``verify`` call before crediting.

Auth: header ``X-API-Key`` on every request. Webhook signature
``X-Webhook-Signature`` = HMAC-SHA256 over the body with the account
``webhookSecret``. The docs sample signs ``JSON.stringify(req.body)``, so we
try the raw bytes first, then the compact canonical re-serialization.
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


class AtlasPayAPIError(Exception):
    """AtlasPay answered with an error status (400/401/404/503)."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f'AtlasPay API error ({status_code}): {message}')


class AtlasPayNetworkError(Exception):
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
    """Kopeks -> whole Toman for the request body (AtlasPay wants integers)."""
    return int(amount_kopeks // _KOPEKS_IN_TOMAN)


class AtlasPayService:
    """REST client for AtlasPay v1."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return (settings.ATLASPAY_BASE_URL or 'https://api.atlaspay.space/api/v1').rstrip('/')

    @property
    def api_key(self) -> str:
        return settings.ATLASPAY_API_KEY or ''

    @property
    def webhook_secret(self) -> str:
        return settings.ATLASPAY_WEBHOOK_SECRET or ''

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
                    logger.error('AtlasPay API error', url=url, status=response.status, message=message)
                    raise AtlasPayAPIError(response.status, message) from error

                if response.status == 404 and allow_404:
                    return None

                if response.status >= 400 or (isinstance(data, dict) and data.get('success') is False):
                    message = self._error_message(data)
                    logger.error('AtlasPay API error', url=url, status=response.status, message=message)
                    raise AtlasPayAPIError(response.status, message)

                return data if isinstance(data, dict) else {'_raw': data}
        except (AtlasPayAPIError, AtlasPayNetworkError):
            raise
        except (aiohttp.ClientError, TimeoutError) as error:
            logger.error('AtlasPay API connection error', url=url, error=str(error))
            raise AtlasPayNetworkError(str(error)) from error

    async def create_order(
        self,
        *,
        merchant_order_ref: str,
        amount_kopeks: int,
        customer_telegram_id: int | None = None,
        webhook_url: str | None = None,
    ) -> dict[str, Any]:
        """POST /orders — creates an order, returns start-link + total + tracking."""
        payload: dict[str, Any] = {
            'merchantOrderRef': merchant_order_ref[:64],
            'baseAmountToman': kopeks_to_toman(amount_kopeks),
        }
        if customer_telegram_id:
            payload['customerTelegramId'] = int(customer_telegram_id)
        if webhook_url:
            payload['webhookUrl'] = webhook_url

        logger.info('AtlasPay create_order', merchant_order_ref=merchant_order_ref, amount_kopeks=amount_kopeks)

        data = await self._request('POST', '/orders', json_payload=payload)
        if not data or data.get('orderId') is None or not data.get('customerStartLink'):
            logger.error(
                'AtlasPay create_order: incomplete response',
                merchant_order_ref=merchant_order_ref,
                response_data=data,
            )
            raise AtlasPayAPIError(201, f'Incomplete create order response: {self._error_message(data)}')

        logger.info(
            'AtlasPay order created',
            merchant_order_ref=merchant_order_ref,
            order_id=data.get('orderId'),
        )
        return data

    async def get_order(self, *, order_id: str | int) -> dict[str, Any] | None:
        """GET /orders/{id} — current order state (for polling)."""
        if order_id is None or str(order_id) == '':
            raise ValueError('AtlasPay get_order: order_id is required')
        return await self._request('GET', f'/orders/{order_id}', allow_404=True)

    async def verify_order(self, *, order_id: str | int) -> dict[str, Any] | None:
        """POST /orders/{id}/verify — final check with the authoritative ``paid`` flag.

        NOTE: ``paid: true`` alone is NOT enough — the caller must also require
        ``requiresManualDelivery`` to be false (accepted underpayment trap).
        """
        if order_id is None or str(order_id) == '':
            raise ValueError('AtlasPay verify_order: order_id is required')
        return await self._request('POST', f'/orders/{order_id}/verify', allow_404=True)

    # ------------------------------------------------------------------
    # Webhook signature
    # ------------------------------------------------------------------

    @staticmethod
    def parse_callback_body(raw_body: bytes) -> dict[str, Any] | None:
        """Parses the webhook JSON body into a dict."""
        try:
            data = json.loads(raw_body)
        except (ValueError, TypeError) as error:
            logger.error('AtlasPay callback: failed to parse JSON', error=str(error))
            return None
        if not isinstance(data, dict):
            logger.error('AtlasPay callback: body is not a JSON object')
            return None
        return data

    def _expected_signatures(self, raw_body: bytes, parsed: dict[str, Any]) -> list[str]:
        """Candidate HMACs: raw bytes first, then compact canonical JSON.

        The docs sample signs ``JSON.stringify(req.body)`` (compact, received
        key order, unescaped unicode) — which usually equals the raw bytes,
        but proxies may reformat. Trying both keeps verification robust.
        """
        secret = self.webhook_secret.encode('utf-8')
        candidates = [hmac.new(secret, raw_body, hashlib.sha256).hexdigest()]
        try:
            canonical = json.dumps(parsed, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
            candidates.append(hmac.new(secret, canonical, hashlib.sha256).hexdigest())
        except (ValueError, TypeError):
            pass
        return candidates

    def verify_callback_signature(
        self,
        raw_body: bytes,
        parsed: dict[str, Any],
        signature: str | None,
    ) -> bool:
        """Verifies X-Webhook-Signature — HMAC-SHA256 with the account secret."""
        try:
            received = (signature or '').strip().lower()
            if not received:
                logger.warning('AtlasPay callback: missing X-Webhook-Signature')
                return False
            if not self.webhook_secret:
                logger.error('AtlasPay callback: webhook secret is not set, verification impossible')
                return False
            for expected in self._expected_signatures(raw_body, parsed):
                if hmac.compare_digest(expected, received):
                    return True
            logger.warning('AtlasPay callback: invalid signature')
            return False
        except Exception as error:
            logger.error('AtlasPay callback verify error', error=str(error))
            return False


# Singleton instance
atlaspay_service = AtlasPayService()
