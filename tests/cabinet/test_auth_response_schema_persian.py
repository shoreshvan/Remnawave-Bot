from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app.cabinet.routes.auth import _user_to_response, router
from app.cabinet.schemas.auth import AuthResponse, CampaignBonusInfo, TokenResponse, UserAvatarResponse, UserResponse


@pytest.mark.parametrize('model', [AuthResponse, CampaignBonusInfo, TokenResponse, UserAvatarResponse, UserResponse])
def test_response_documentation(model):
    schema = model.model_json_schema()
    for entry in [schema, *schema['properties'].values()]:
        if '$ref' in entry and 'title' not in entry:
            entry = schema['$defs'][entry['$ref'].rsplit('/', 1)[1]]
        assert any('\u0600' <= char <= '\u06ff' for char in entry['title'])
    assert any('\u0600' <= char <= '\u06ff' for char in schema['description'])


@pytest.mark.parametrize('amount', [0, 1, 125050])
def test_response_preserves_money_and_custom_data(amount):
    created = datetime(2026, 1, 1, tzinfo=UTC)
    source = dict(id=123, telegram_id=456, username='custom_user', first_name='نام مدیر',
        last_name=None, email='test@example.com', email_verified=True, balance_kopeks=amount,
        balance_rubles=amount / 100, referral_code='ABC123', language='fa', created_at=created, auth_type='telegram')
    user = _user_to_response(SimpleNamespace(**source))
    assert user.model_dump() == source
    bonus = CampaignBonusInfo(campaign_name='Custom campaign', bonus_type='balance', balance_kopeks=amount,
                              subscription_days=7, tariff_name='Custom tariff')
    response = AuthResponse(access_token='access', refresh_token='refresh', expires_in=3600, user=user, campaign_bonus=bonus)
    payload = response.model_dump()
    assert payload['user'] == source
    assert payload['token_type'] == 'bearer' and payload['access_token'] == 'access'
    assert payload['refresh_token'] == 'refresh' and payload['expires_in'] == 3600
    assert payload['campaign_bonus'] == {'campaign_name': 'Custom campaign', 'bonus_type': 'balance',
        'balance_kopeks': amount, 'subscription_days': 7, 'tariff_name': 'Custom tariff'}


def test_defaults_and_attributes_preserved():
    user = UserResponse.model_validate(SimpleNamespace(id=123, created_at=datetime.now(UTC)))
    assert user.language == 'ru' and user.auth_type == 'telegram'
    assert user.balance_kopeks == 0 and user.balance_rubles == 0.0 and user.email is None
    assert TokenResponse(access_token='a', refresh_token='r', expires_in=60).token_type == 'bearer'
    assert UserAvatarResponse().photo_url is None


def test_nested_openapi_titles():
    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()
    response = schema['paths']['/auth/deeplink/poll']['post']['responses']['200']
    assert response['content']['application/json']['schema']['$ref'].endswith('/AuthResponse')
    models = schema['components']['schemas']
    for name in ['AuthResponse', 'UserResponse', 'CampaignBonusInfo']:
        assert any('\u0600' <= char <= '\u06ff' for char in models[name]['title'])
