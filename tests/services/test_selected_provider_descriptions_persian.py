"""Render actual localized description expressions for the selected providers."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.localization.texts import get_texts
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.parametrize('provider', ['atlaspay', 'hooshpay'])
@pytest.mark.parametrize(('telegram_id', 'suffix'), [(123, ' (ID 123)'), (None, ' (U456)')])
@pytest.mark.parametrize('custom_label', [None, 'اعتبار سفارشی'])
def test_cabinet_invoice_amount_description(persian, monkeypatch, provider, telegram_id, suffix, custom_label):
    path = Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes' / 'balance.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == f'create_{provider}_payment']
    assert len(calls) == 1
    expression = next(keyword.value for keyword in calls[0].keywords if keyword.arg == 'description')
    monkeypatch.setattr(settings, 'PAYMENT_BALANCE_TEMPLATE', '{service_name} | {description}')
    monkeypatch.setattr(settings, 'PAYMENT_SERVICE_NAME', 'سرویس آزمایشی')
    monkeypatch.setattr(settings, 'CURRENCY_SYMBOL', 'تومان')
    monkeypatch.setattr(settings, 'PAYMENT_BALANCE_DESCRIPTION',
                        custom_label if custom_label is not None
                        else type(settings).model_fields['PAYMENT_BALANCE_DESCRIPTION'].default)
    context = dict(settings=settings, user=SimpleNamespace(language='fa', telegram_id=telegram_id, id=456),
                   request=SimpleNamespace(amount_kopeks=250045))
    rendered = eval(compile(ast.Expression(expression), str(path), 'eval'), context)
    label = custom_label if custom_label is not None else 'شارژ موجودی'
    assert rendered == f'سرویس آزمایشی | {label} به مبلغ {settings.format_price(250045, round_kopeks=False)}{suffix}'
    assert 'تومان' in rendered and '₽' not in rendered
    assert '2,500.45' in rendered


def test_cabinet_nowpayments_invoice_description(persian, monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes' / 'balance.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == 'create_nowpayments_payment']
    assert len(calls) == 1
    expression = next(keyword.value for keyword in calls[0].keywords if keyword.arg == 'description')
    monkeypatch.setattr(settings, 'PAYMENT_BALANCE_TEMPLATE', '{service_name} | {description}')
    monkeypatch.setattr(settings, 'PAYMENT_SERVICE_NAME', 'سرویس آزمایشی')
    rendered = eval(compile(ast.Expression(expression), str(path), 'eval'), dict(settings=settings, texts=persian))
    assert rendered == 'سرویس آزمایشی | شارژ موجودی'


@pytest.mark.parametrize('provider', ['atlaspay', 'hooshpay', 'nowpayments', 'tonpays'])
def test_invoice_and_credit_description_expressions(persian, provider):
    path = Path(__file__).resolve().parents[2] / 'app' / 'services' / 'payment' / f'{provider}.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    expressions = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'description' for target in node.targets)
    ]
    context = dict(
        get_texts=get_texts, texts=persian, language='fa', user=SimpleNamespace(language='fa'),
        settings=settings, display_name='درگاه انتخابی',
    )
    rendered = [
        eval(compile(ast.Expression(expr), str(path), 'eval'), context)
        for expr in sorted(expressions, key=lambda expr: expr.lineno)
    ]
    assert rendered == ['شارژ موجودی', 'شارژ از طریق درگاه انتخابی']


@pytest.mark.parametrize('provider', ['atlaspay', 'hooshpay', 'nowpayments', 'tonpays'])
def test_success_notification_expression(persian, provider):
    path = Path(__file__).resolve().parents[2] / 'app' / 'services' / 'payment' / f'{provider}.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    messages = [node.args[1] for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'send_message' and len(node.args) > 1
                and any(isinstance(value, ast.Constant) and value.value == 'NOWPAYMENTS_TOPUP_SUCCESS'
                        for value in ast.walk(node.args[1]))]
    assert len(messages) == 1
    context = dict(get_texts=get_texts, texts=persian, settings=settings,
                   user=SimpleNamespace(language='fa'), payment=SimpleNamespace(amount_kopeks=250000),
                   transaction=SimpleNamespace(id=123), display_name='درگاه انتخابی')
    rendered = eval(compile(ast.Expression(messages[0]), str(path), 'eval'), context)
    assert rendered == persian.t('NOWPAYMENTS_TOPUP_SUCCESS').format(
        amount=settings.format_price(250000), method='درگاه انتخابی', transaction_id=123,
    )
