import asyncio

import structlog
from sqlalchemy import select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import AtlasPayPayment
from app.services.payment.atlaspay import ATLASPAY_PENDING_STATUSES


logger = structlog.get_logger(__name__)


class AtlasPayPollingService:
    def __init__(self):
        self._task = None

    async def start(self, payment_service):
        await self.stop()
        self._task = asyncio.create_task(self._run(payment_service))

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self, payment_service):
        while True:
            try:
                if settings.is_atlaspay_enabled():
                    await self.check_pending(payment_service)
            except Exception:
                logger.exception('AtlasPay polling pass failed')
            await asyncio.sleep(30)

    async def check_pending(self, payment_service):
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AtlasPayPayment.order_id)
                .where(
                    AtlasPayPayment.is_paid.is_(False),
                    AtlasPayPayment.status.in_(ATLASPAY_PENDING_STATUSES),
                )
                .order_by(AtlasPayPayment.created_at)
            )
            order_ids = list(result.scalars().all())
        for order_id in order_ids:
            try:
                async with AsyncSessionLocal() as db:
                    outcome = await payment_service.check_atlaspay_payment_status(db, order_id, polling=True)
                    if outcome:
                        await db.commit()
                    else:
                        await db.rollback()
            except Exception:
                logger.exception('AtlasPay polling order failed', order_id=order_id)


atlaspay_polling_service = AtlasPayPollingService()
