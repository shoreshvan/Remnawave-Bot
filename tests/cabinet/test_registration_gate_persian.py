"""Persian registration denial preserves access decisions and machine codes."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.cabinet.auth.registration_access import raise_for_registration_decision
from app.cabinet.utils.request_language import get_request_language
from app.services.registration_access_service import RegistrationAccessDecision, RegistrationAccessReason
from tests.fixtures.persian import persian  # noqa: F401


@pytest.mark.parametrize('reason', list(RegistrationAccessReason))
def test_registration_decision(persian, reason):
    assert raise_for_registration_decision(RegistrationAccessDecision(True, reason), language='fa') is None
    with pytest.raises(HTTPException) as error:
        raise_for_registration_decision(RegistrationAccessDecision(False, reason), language='fa')
    if reason == RegistrationAccessReason.BLOCKED:
        assert error.value.status_code == 403
        assert error.value.detail == persian.t('CABINET_AUTH_ACCOUNT_NOT_ACTIVE')
    elif reason == RegistrationAccessReason.CHECK_UNAVAILABLE:
        assert error.value.status_code == 503
        assert error.value.detail == {'code': 'registration_check_unavailable'}
    else:
        assert error.value.status_code == 403
        assert error.value.detail == {'code': 'registration_invite_required'}


@pytest.mark.parametrize('filename', ['auth.py', 'oauth.py', 'landing.py'])
def test_callers_pass_recipient_language(persian, filename):
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes' / filename
    tree = ast.parse(path.read_text(encoding='utf-8'))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'raise_for_registration_decision']
    assert len(calls) == (1 if filename == 'oauth.py' else 2)
    decision = object()
    capture = Mock()
    context = dict(raise_for_registration_decision=capture, decision=decision, email_access=decision,
                   user=SimpleNamespace(language='fa'), existing_user=SimpleNamespace(language='fa'),
                   request=SimpleNamespace(language=None),
                   raw_request=SimpleNamespace(headers={'Accept-Language': 'fa-IR'}),
                   get_request_language=get_request_language)
    for call in calls:
        eval(compile(ast.Expression(call), str(path), 'eval'), context)
        capture.assert_called_with(decision, language='fa')
