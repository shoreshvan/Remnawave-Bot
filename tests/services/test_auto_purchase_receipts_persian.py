"""Render production receipt expressions and verify the device debit boundary."""

import ast
import html
import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import subscription_auto_purchase_service as auto
from tests.fixtures.persian import persian  # noqa: F401


def test_daily_receipt_shows_exact_amount_charged(persian):
    tree = ast.parse(inspect.getsource(auto._auto_purchase_daily_tariff))
    receipt = next(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == 'message' and 'DAILY_NOTICE_006' in ast.unparse(node.value)
    )
    message = eval(compile(ast.Expression(receipt), '<receipt>', 'eval'), {
        'texts': persian, 'html': html, 'tariff': SimpleNamespace(name='ویژه'), 'final_price': 1234567,
    })
    assert 'مبلغ کسرشده برای روز اول: 12,345.67 تومان' in message


@pytest.mark.parametrize('count', [1, 30, 100])
def test_production_receipt_templates(persian, count):
    # Evaluate the actual production expressions, including their format arguments.
    # Full payment workflows are tested separately; this does not simulate payments.
    expected = {
        '_auto_purchase_tariff': [f'خرید تعرفهٔ «ویژه» به مدت {count} روز', 'بازگشت وجه به‌دلیل خطا در خرید خودکار تعرفه'],
        '_auto_purchase_daily_tariff': ['فعال‌سازی تعرفهٔ روزانهٔ «ویژه»'],
        '_auto_add_devices': [f'خرید {count} دستگاه اضافی'],
        '_auto_add_traffic': [f'خرید {count} گیگابایت ترافیک'],
        '_try_auto_extend_expired_after_topup': [f'تمدید خودکار اشتراک منقضی‌شده به مدت {count} روز'],
        'try_resume_disabled_daily_after_topup': ['پرداخت روزانهٔ تعرفهٔ «ویژه» (ازسرگیری خودکار)'],
    }
    context = {
        'get_texts': auto.get_texts, 'user': SimpleNamespace(language='fa'),
        'tariff': SimpleNamespace(name='ویژه'), 'period_days': count,
        'devices_to_add': count, 'traffic_gb': count,
    }
    for function_name, expected_values in expected.items():
        tree = ast.parse(inspect.getsource(getattr(auto, function_name)))
        assignments = sorted((
            node for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in ('description', 'refund_description')
                    for target in node.targets)
        ), key=lambda node: node.lineno)
        values = [eval(compile(ast.Expression(node.value), '<receipt>', 'eval'), context) for node in assignments]
        assert values == expected_values


@pytest.mark.asyncio
async def test_device_debit_uses_persian_receipt_and_recalculated_price(persian, monkeypatch):
    from app.database.crud import user as user_crud

    user = SimpleNamespace(id=1, telegram_id=1, language='fa', balance_kopeks=100000)
    subscription = SimpleNamespace(
        id=7, device_limit=2, status='active', tariff_id=None,
        end_date=datetime.now(UTC) + timedelta(days=30),
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = subscription
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
    debit = AsyncMock(return_value=False)
    monkeypatch.setattr(user_crud, 'subtract_user_balance', debit)
    monkeypatch.setattr(type(auto.settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(auto.settings, 'PRICE_PER_DEVICE', 1000)
    monkeypatch.setattr(auto.settings, 'MAX_DEVICES_LIMIT', 10)
    monkeypatch.setattr(auto.PricingEngine, 'get_addon_discount_percent', lambda *args: 10)
    success = await auto._auto_add_devices(db, user, {'devices_to_add': 2, 'price_kopeks': 2000})
    assert success is False
    assert debit.await_args.args[2:4] == (1800, 'خرید 2 دستگاه اضافی')
    assert subscription.device_limit == 2
