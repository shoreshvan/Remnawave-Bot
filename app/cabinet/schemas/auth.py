"""Authentication schemas for cabinet."""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.localization.texts import get_texts


class TelegramAuthRequest(BaseModel):
    """Request for Telegram WebApp initData authentication."""

    init_data: str = Field(
        ..., max_length=4096,
        description=get_texts().t('CABINET_AUTH_INIT_DATA_DESCRIPTION', 'Telegram WebApp initData string'),
    )
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$',
        description=get_texts().t('CABINET_AUTH_CAMPAIGN_SLUG_DESCRIPTION', 'Campaign slug from web link'),
    )
    referral_code: str | None = Field(
        None, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$',
        description=get_texts().t('CABINET_AUTH_REFERRAL_CODE_DESCRIPTION', 'Referral code of inviter'),
    )
    accepted_legal_documents: list[str] | None = Field(
        None,
        max_length=8,
        description=get_texts().t(
            'CABINET_AUTH_ACCEPTED_LEGAL_DOCUMENTS_DESCRIPTION',
            'Ключи документов, с которыми пользователь согласился на экране первой авторизации '
            '(см. GET /cabinet/info/legal-consent). Нужны только при создании НОВОГО аккаунта; '
            'для существующего игнорируются.',
        ),
    )


class TelegramWidgetAuthRequest(BaseModel):
    """Request for Telegram Login Widget authentication."""

    id: int = Field(..., description=get_texts().t('CABINET_AUTH_TELEGRAM_ID_DESCRIPTION', 'Telegram user ID'))
    first_name: str = Field(
        ..., max_length=64,
        description=get_texts().t('CABINET_AUTH_WIDGET_FIRST_NAME_DESCRIPTION', "User's first name"),
    )
    last_name: str | None = Field(
        None, max_length=64,
        description=get_texts().t('CABINET_AUTH_WIDGET_LAST_NAME_DESCRIPTION', "User's last name"),
    )
    username: str | None = Field(
        None, max_length=32,
        description=get_texts().t('CABINET_AUTH_WIDGET_USERNAME_DESCRIPTION', "User's username"),
    )
    photo_url: str | None = Field(
        None, max_length=512,
        description=get_texts().t('CABINET_AUTH_WIDGET_PHOTO_URL_DESCRIPTION', "User's photo URL"),
    )
    auth_date: int = Field(
        ..., description=get_texts().t('CABINET_AUTH_AUTH_DATE_DESCRIPTION', 'Unix timestamp of authentication')
    )
    hash: str = Field(
        ..., min_length=64, max_length=64,
        description=get_texts().t('CABINET_AUTH_HASH_DESCRIPTION', 'Authentication hash'),
    )
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$',
        description=get_texts().t('CABINET_AUTH_CAMPAIGN_SLUG_DESCRIPTION', 'Campaign slug from web link'),
    )
    referral_code: str | None = Field(
        None, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$',
        description=get_texts().t('CABINET_AUTH_REFERRAL_CODE_DESCRIPTION', 'Referral code of inviter'),
    )
    accepted_legal_documents: list[str] | None = Field(
        None,
        max_length=8,
        description=get_texts().t(
            'CABINET_AUTH_ACCEPTED_LEGAL_DOCUMENTS_DESCRIPTION',
            'Ключи документов, с которыми пользователь согласился на экране первой авторизации '
            '(см. GET /cabinet/info/legal-consent). Нужны только при создании НОВОГО аккаунта; '
            'для существующего игнорируются.',
        ),
    )


class TelegramOIDCAuthRequest(BaseModel):
    """Request for Telegram OIDC authentication (popup flow)."""

    id_token: str = Field(
        ..., max_length=4096,
        description=get_texts().t('CABINET_AUTH_ID_TOKEN_DESCRIPTION', 'JWT id_token from Telegram OIDC popup'),
    )
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$',
        description=get_texts().t('CABINET_AUTH_CAMPAIGN_SLUG_DESCRIPTION', 'Campaign slug from web link'),
    )
    referral_code: str | None = Field(
        None, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$',
        description=get_texts().t('CABINET_AUTH_REFERRAL_CODE_DESCRIPTION', 'Referral code of inviter'),
    )
    accepted_legal_documents: list[str] | None = Field(
        None,
        max_length=8,
        description=get_texts().t(
            'CABINET_AUTH_ACCEPTED_LEGAL_DOCUMENTS_DESCRIPTION',
            'Ключи документов, с которыми пользователь согласился на экране первой авторизации '
            '(см. GET /cabinet/info/legal-consent). Нужны только при создании НОВОГО аккаунта; '
            'для существующего игнорируются.',
        ),
    )


class EmailRegisterRequest(BaseModel):
    """Request to register/link email to existing Telegram account."""

    email: EmailStr = Field(..., description=get_texts().t('CABINET_AUTH_EMAIL_DESCRIPTION', 'Email address'))
    password: str = Field(
        ..., min_length=8, max_length=128,
        description=get_texts().t('CABINET_AUTH_PASSWORD_MIN8_DESCRIPTION', 'Password (min 8 chars)'),
    )


class EmailVerifyRequest(BaseModel):
    """Request to verify email with token."""

    token: str = Field(
        ..., max_length=2048,
        description=get_texts().t('CABINET_AUTH_EMAIL_VERIFICATION_TOKEN_DESCRIPTION', 'Email verification token'),
    )
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$',
        description=get_texts().t('CABINET_AUTH_CAMPAIGN_SLUG_DESCRIPTION', 'Campaign slug from web link'),
    )


class EmailLoginRequest(BaseModel):
    """Request to login with email and password."""

    email: EmailStr = Field(..., description=get_texts().t('CABINET_AUTH_EMAIL_DESCRIPTION', 'Email address'))
    password: str = Field(
        ..., min_length=1, max_length=128,
        description=get_texts().t('CABINET_AUTH_PASSWORD_DESCRIPTION', 'Password'),
    )
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$',
        description=get_texts().t('CABINET_AUTH_CAMPAIGN_SLUG_DESCRIPTION', 'Campaign slug from web link'),
    )


class RefreshTokenRequest(BaseModel):
    """Request to refresh access token."""

    refresh_token: str = Field(
        ..., max_length=2048,
        description=get_texts().t('CABINET_AUTH_REFRESH_TOKEN_DESCRIPTION', 'Refresh token'),
    )


class VerificationResendRequest(BaseModel):
    """Request to resend the verification email from the «check your inbox» screen."""

    email: EmailStr = Field(..., description='Email address awaiting verification')


class PasswordForgotRequest(BaseModel):
    """Request to initiate password reset."""

    email: EmailStr = Field(..., description=get_texts().t('CABINET_AUTH_EMAIL_DESCRIPTION', 'Email address'))


class PasswordResetRequest(BaseModel):
    """Request to reset password with token."""

    token: str = Field(
        ..., max_length=2048,
        description=get_texts().t('CABINET_AUTH_PASSWORD_RESET_TOKEN_DESCRIPTION', 'Password reset token'),
    )
    password: str = Field(
        ..., min_length=8, max_length=128,
        description=get_texts().t('CABINET_AUTH_NEW_PASSWORD_DESCRIPTION', 'New password (min 8 chars)'),
    )


class AutoLoginRequest(BaseModel):
    """Request for auto-login from guest purchase success page."""

    token: str = Field(
        ..., max_length=2048,
        description=get_texts().t('CABINET_AUTH_AUTO_LOGIN_TOKEN_DESCRIPTION', 'Auto-login JWT token'),
    )


class TokenResponse(BaseModel):
    """Token pair response."""

    model_config = {'title': 'توکن‌های ورود', 'json_schema_extra': {'description': 'توکن دسترسی و توکن تمدید نشست'}}

    access_token: str = Field(..., title='توکن دسترسی')
    refresh_token: str = Field(..., title='توکن تمدید نشست')
    token_type: str = Field('bearer', title='نوع توکن')
    expires_in: int = Field(
        ...,
        title='مدت اعتبار', description=get_texts('fa').t('CABINET_AUTH_EXPIRES_IN_DESCRIPTION', 'Access token expiration in seconds'),
    )


class UserResponse(BaseModel):
    """User data response."""

    id: int = Field(..., title='شناسهٔ کاربر')
    telegram_id: int | None = Field(None, title='شناسهٔ تلگرام')  # Nullable для email-only пользователей
    username: str | None = Field(None, title='نام کاربری')
    first_name: str | None = Field(None, title='نام')
    last_name: str | None = Field(None, title='نام خانوادگی')
    email: str | None = Field(None, title='نشانی ایمیل')
    email_verified: bool = Field(False, title='تأییدشدن ایمیل')
    balance_kopeks: int = Field(0, title='موجودی در مقیاس ذخیره‌سازی')
    balance_rubles: float = Field(0.0, title='موجودی در مقیاس نمایشی')
    referral_code: str | None = Field(None, title='کد دعوت')
    language: str = Field('ru', title='زبان حساب')
    created_at: datetime = Field(..., title='زمان ایجاد حساب')
    auth_type: str = Field('telegram', title='روش ورود')  # "telegram" или "email"

    class Config:
        from_attributes = True
        title = 'اطلاعات کاربر'
        json_schema_extra = {'description': 'اطلاعات حساب کاربر واردشده'}


class UserAvatarResponse(BaseModel):
    """Фото профиля Telegram для шапки кабинета: подписанная ссылка на прокси медиа или null."""

    model_config = {'title': 'تصویر کاربر', 'json_schema_extra': {'description': 'نشانی تصویر نمایه یا مقدار خالی در نبود تصویر'}}

    photo_url: str | None = Field(None, title='نشانی تصویر')


class CampaignBonusInfo(BaseModel):
    """Info about campaign bonus applied during auth."""

    model_config = {'title': 'پاداش کمپین', 'json_schema_extra': {'description': 'اطلاعات پاداش اعمال‌شده هنگام ورود'}}

    campaign_name: str = Field(..., title='نام کمپین')
    bonus_type: str = Field(..., title='نوع پاداش')
    balance_kopeks: int = Field(0, title='مبلغ پاداش در مقیاس ذخیره‌سازی')
    subscription_days: int | None = Field(None, title='تعداد روز اشتراک')
    tariff_name: str | None = Field(None, title='نام تعرفه')


class AuthResponse(BaseModel):
    """Full authentication response with tokens and user."""

    model_config = {'title': 'نتیجهٔ ورود', 'json_schema_extra': {'description': 'توکن‌های نشست، اطلاعات کاربر و پاداش احتمالی کمپین'}}

    access_token: str = Field(..., title='توکن دسترسی')
    refresh_token: str = Field(..., title='توکن تمدید نشست')
    token_type: str = Field('bearer', title='نوع توکن')
    expires_in: int = Field(..., title='مدت اعتبار', description='مدت اعتبار توکن دسترسی بر حسب ثانیه')
    user: UserResponse = Field(..., title='اطلاعات کاربر')
    campaign_bonus: CampaignBonusInfo | None = Field(None, title='پاداش کمپین')


class EmailRegisterStandaloneRequest(BaseModel):
    """Request to register new account with email (no Telegram required)."""

    email: EmailStr = Field(..., description=get_texts().t('CABINET_AUTH_EMAIL_DESCRIPTION', 'Email address'))
    password: str = Field(
        ..., min_length=8, max_length=128,
        description=get_texts().t('CABINET_AUTH_PASSWORD_MIN8_DESCRIPTION', 'Password (min 8 chars)'),
    )
    first_name: str | None = Field(
        None, max_length=64,
        description=get_texts().t('CABINET_AUTH_REGISTER_FIRST_NAME_DESCRIPTION', 'First name'),
    )
    language: str = Field(
        'ru', max_length=5, pattern=r'^[a-z]{2}$',
        description=get_texts().t('CABINET_AUTH_PREFERRED_LANGUAGE_DESCRIPTION', 'Preferred language (ISO 639-1)'),
    )
    referral_code: str | None = Field(
        None, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$',
        description=get_texts().t('CABINET_AUTH_REFERRAL_CODE_DESCRIPTION', 'Referral code of inviter'),
    )
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$',
        description=get_texts().t('CABINET_AUTH_CAMPAIGN_SLUG_DESCRIPTION', 'Campaign slug from web link'),
    )
    accepted_legal_documents: list[str] | None = Field(
        None,
        max_length=8,
        description=get_texts().t(
            'CABINET_AUTH_ACCEPTED_LEGAL_DOCUMENTS_SHORT_DESCRIPTION',
            'Ключи документов, с которыми пользователь согласился на экране первой авторизации '
            '(см. GET /cabinet/info/legal-consent).',
        ),
    )


class RegisterResponse(BaseModel):
    """Response for email registration (before verification)."""

    message: str = Field(..., description=get_texts().t('CABINET_AUTH_SUCCESS_MESSAGE_DESCRIPTION', 'Success message'))
    email: str = Field(
        ...,
        description=get_texts().t('CABINET_AUTH_EMAIL_TO_VERIFY_DESCRIPTION', 'Email address to verify'),
    )
    requires_verification: bool = Field(
        True,
        description=get_texts().t(
            'CABINET_AUTH_REQUIRES_VERIFICATION_DESCRIPTION', 'Whether email verification is required'
        ),
    )


class EmailChangeRequest(BaseModel):
    """Request to initiate email change."""

    model_config = {'title': 'درخواست تغییر ایمیل', 'json_schema_extra': {'description': 'ثبت درخواست تغییر نشانی ایمیل حساب'}}

    new_email: EmailStr = Field(
        ...,
        title='ایمیل جدید',
        description=get_texts('fa').t('CABINET_AUTH_NEW_EMAIL_DESCRIPTION', 'New email address'),
    )


class EmailChangeVerifyRequest(BaseModel):
    """Request to verify email change with code."""

    model_config = {'title': 'تأیید تغییر ایمیل', 'json_schema_extra': {'description': 'تأیید تغییر ایمیل با کد ارسال‌شده به نشانی جدید'}}

    code: str = Field(
        ..., min_length=6, max_length=6, pattern=r'^\d{6}$',
        title='کد تأیید',
        description=get_texts('fa').t('CABINET_AUTH_VERIFICATION_CODE_DESCRIPTION', '6-digit verification code'),
    )


class EmailMergeVerifyRequest(BaseModel):
    """Request to confirm an email account merge with the emailed code."""

    model_config = {'title': 'تأیید ادغام حساب', 'json_schema_extra': {'description': 'تأیید مالکیت ایمیل برای ادغام حساب‌ها با کد ارسالی'}}

    code: str = Field(
        ..., min_length=6, max_length=6, pattern=r'^\d{6}$',
        title='کد تأیید',
        description=get_texts('fa').t('CABINET_AUTH_CONFIRMATION_CODE_DESCRIPTION', '6-digit confirmation code'),
    )


class EmailChangeResponse(BaseModel):
    """Response for email change initiation."""

    model_config = {'title': 'نتیجهٔ درخواست تغییر ایمیل', 'json_schema_extra': {'description': 'نتیجهٔ درخواست، نشانی جدید و مدت اعتبار کد تأیید'}}

    message: str = Field(..., title='پیام نتیجه', description='نتیجهٔ درخواست تغییر ایمیل و وضعیت ارسال تأییدیه')
    new_email: str = Field(
        ...,
        title='ایمیل جدید',
        description=get_texts('fa').t(
            'CABINET_AUTH_NEW_EMAIL_PENDING_DESCRIPTION', 'New email address pending verification'
        ),
    )
    expires_in_minutes: int = Field(
        ...,
        title='مدت اعتبار کد',
        description=get_texts('fa').t(
            'CABINET_AUTH_CODE_EXPIRATION_MINUTES_DESCRIPTION', 'Code expiration time in minutes'
        ),
    )


class DeepLinkTokenResponse(BaseModel):
    """Response with deep link auth token."""

    model_config = {'title': 'لینک ورود تلگرام', 'json_schema_extra': {'description': 'توکن ورود، نام ربات و مدت اعتبار لینک'}}

    token: str = Field(
        ...,
        title='توکن ورود', description=get_texts('fa').t('CABINET_AUTH_ONE_TIME_TOKEN_DESCRIPTION', 'One-time auth token'),
    )
    bot_username: str = Field(
        ...,
        title='نام کاربری ربات', description=get_texts('fa').t('CABINET_AUTH_BOT_USERNAME_DESCRIPTION', 'Bot username for deep link'),
    )
    expires_in: int = Field(
        ...,
        title='مدت اعتبار', description='مدت اعتبار لینک ورود بر حسب ثانیه',
    )


class DeepLinkPollRequest(BaseModel):
    """Request to poll deep link auth status.

    Deep link auth is always for existing bot users — referral codes are not applicable here.
    Only campaign_slug is supported (campaign bonus can apply to existing users).
    """

    model_config = {'title': 'پیگیری ورود تلگرام', 'json_schema_extra': {'description': 'توکن لینک ورود و شناسهٔ اختیاری کمپین'}}

    token: str = Field(
        ..., min_length=16, max_length=128,
        title='توکن ورود', description=get_texts('fa').t('CABINET_AUTH_DEEP_LINK_TOKEN_DESCRIPTION', 'Deep link auth token'),
    )
    campaign_slug: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        pattern=r'^[a-zA-Z0-9_-]+$',
        title='شناسهٔ کمپین', description=get_texts('fa').t(
            'CABINET_AUTH_CAMPAIGN_SLUG_CABINET_URL_DESCRIPTION',
            'Campaign slug captured from cabinet URL',
        ),
    )
