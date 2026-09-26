"""HTTP client for Lazeika VPN support API."""

import uuid
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class LazeikaClient:
    """Client for communicating with Lazeika VPN support and migration API."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or settings.LAZEIKA_API_URL or 'https://bot.lazeika.xyz').rstrip('/')
        self.api_key = api_key or settings.LAZEIKA_API_KEY
        self.timeout = aiohttp.ClientTimeout(total=10)

    def _headers(self) -> dict[str, str]:
        return {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) InvoxyMigration/1.0',
            'Authorization': f'Bearer {self.api_key}',
            'x-support-request-id': str(uuid.uuid4()),
            'Accept': 'application/json',
        }

    async def get_client_context(
        self,
        *,
        telegram_id: int | str | None = None,
        email: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch client context from Lazeika by telegram_id or email."""
        if not telegram_id and not email:
            return None

        params: dict[str, str] = {}
        if telegram_id:
            params['telegramId'] = str(telegram_id)
        if email:
            params['email'] = str(email).strip().lower()

        url = f'{self.base_url}/internal/support/client-context'
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, params=params, headers=self._headers()) as resp:
                    if resp.status != 200:
                        logger.warning('Lazeika client-context returned non-200', status=resp.status)
                        return None
                    data = await resp.json()
                    return data
        except Exception as exc:
            logger.error('Failed to query Lazeika client-context', error=str(exc))
            return None

    async def annul_subscription(
        self,
        *,
        client_id: str | None = None,
        subscription_id: str | None = None,
        telegram_id: int | str | None = None,
        reason: str = 'Мигрирован в Invoxy VPN (@invoxy_bot / invoxy.my)',
    ) -> bool:
        """Annuls subscription and blocks old client in Lazeika database."""
        if not client_id and not subscription_id:
            return False

        payload = {
            'clientId': client_id,
            'subscriptionId': subscription_id,
            'telegramId': str(telegram_id) if telegram_id else None,
            'reason': reason,
        }

        url = f'{self.base_url}/internal/support/annul-subscription'
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(url, json=payload, headers=self._headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return bool(data.get('success'))
                    logger.warning('Lazeika annul-subscription returned non-200', status=resp.status)
                    return False
        except Exception as exc:
            logger.error('Failed to annul Lazeika subscription', error=str(exc))
            return False
