"""Apple In-App Purchase schemas for cabinet."""

from pydantic import BaseModel, Field, field_validator

from app.localization.texts import get_texts


class ApplePurchaseRequest(BaseModel):
    """Request to verify and credit an Apple IAP transaction."""

    product_id: str = Field(
        ...,
        description=get_texts().t(
            'APPLE_IAP_PRODUCT_ID_DESC',
            'Apple product ID (e.g. com.bitnet.vpnclient.topup.100)',
        ),
    )
    transaction_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=get_texts().t('APPLE_IAP_TRANSACTION_ID_DESC', 'Apple StoreKit transaction ID'),
    )

    @field_validator('transaction_id')
    @classmethod
    def transaction_id_must_be_numeric(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError(
                get_texts().t('APPLE_IAP_TRANSACTION_ID_NOT_NUMERIC', 'transaction_id must contain only digits')
            )
        return v


class ApplePurchaseResponse(BaseModel):
    """Response indicating whether the purchase was successfully credited."""

    success: bool


class AppleAccountTokenResponse(BaseModel):
    """Stable StoreKit appAccountToken for the authenticated user."""

    app_account_token: str
