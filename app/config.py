from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Aflex Loans API"
    environment: str = "development"
    api_v1_prefix: str = "/api/v1"
    secret_key: str = "change-me"
    jwt_secret: str | None = None
    refresh_secret_key: str = "change-me-refresh"
    jwt_refresh_secret: str | None = None
    access_token_expire_minutes: int = 120
    refresh_token_expire_minutes: int = 10080
    database_url: str = "sqlite:///./aflex_loans.db"
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800
    login_rate_limit_per_minute: int = 10
    otp_rate_limit_per_minute: int = 6
    upload_provider: str = "cloudinary"
    cloudinary_cloud_name: str | None = None
    cloudinary_api_key: str | None = None
    cloudinary_api_secret: str | None = None
    upload_max_mb: int = 8

    personal_min_amount: int = 500
    personal_max_amount: int = 50000
    personal_term_days: int = 30
    personal_monthly_interest_min: float = 0.0
    personal_monthly_interest_max: float = 0.0
    # 1/6 upfront fee: contract KSh 1,200 disburses KSh 1,000.
    personal_upfront_fee_rate: float = 0.1666666667
    personal_daily_late_interest_rate: float = 0.002

    business_min_amount: int = 20000
    business_max_amount: int = 300000
    business_allowed_terms: str = "30,60,90"
    business_monthly_interest_min: float = 0.10
    business_monthly_interest_max: float = 0.18

    fixed_markup_amount: float = 0.0
    loyalty_limit_increment_rate: float = 0.15
    loyalty_limit_max_multiplier: float = 2.0
    loyalty_limit_min_bonus_amount: float = 1000.0

    processing_fee_min: float = 0.03
    processing_fee_max: float = 0.05
    personal_late_fee: float = 150.0
    business_late_fee: float = 500.0

    max_active_loans_per_user: int = 1
    max_daily_applications_per_user: int = 3
    require_explicit_consent: bool = True

    otp_provider: str = "console"
    otp_sender_id: str | None = None
    otp_allow_debug_code: bool = True
    otp_require_real_delivery: bool = False
    otp_africastalking_username: str | None = None
    otp_africastalking_api_key: str | None = None
    otp_twilio_account_sid: str | None = None
    otp_twilio_auth_token: str | None = None
    otp_twilio_sms_from: str | None = None
    otp_twilio_whatsapp_from: str | None = None
    otp_twilio_voice_from: str | None = None
    otp_smtp_host: str | None = None
    otp_smtp_port: int = 587
    otp_smtp_username: str | None = None
    otp_smtp_password: str | None = None
    otp_smtp_from_email: str | None = None
    otp_smtp_use_tls: bool = True
    otp_test_redirect_email: str | None = None
    loan_admin_alert_email: str | None = None
    loan_user_notification_email_override: str | None = None
    mpesa_collection_phone: str = "0721802110"
    due_reminder_enabled: bool = True
    due_reminder_days_before: int = 10
    due_reminder_scan_interval_minutes: int = 60
    distribution_public_base_url: str | None = None
    distribution_install_page_path: str = "/install"
    distribution_apk_filename: str = "aflex-loans-latest.apk"
    distribution_downloads_dir: str = "./downloads"
    distribution_brand_name: str = "Aflex Loan"
    distribution_whatsapp_share_text: str = (
        "Install Aflex Loan app here: {install_url} . Download APK directly: {apk_url}"
    )
    distribution_support_phone: str | None = None
    distribution_support_email: str | None = None
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None
    bootstrap_admin_full_name: str = "Aflex Loans Admin"
    bootstrap_admin_phone: str | None = None

    compliance_disclaimer: str = Field(
        default=(
            "This platform must operate under Kenyan law, including CBK digital credit and "
            "Data Protection requirements. Obtain legal counsel before go-live."
        )
    )

    @staticmethod
    def _parse_csv(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        return self._parse_csv(self.cors_origins)

    @property
    def business_allowed_terms_list(self) -> list[int]:
        return [int(value) for value in self._parse_csv(self.business_allowed_terms)]

    @property
    def normalized_database_url(self) -> str:
        url = (self.database_url or "").strip()
        if url.startswith("postgres://"):
            return "postgresql+psycopg2://" + url[len("postgres://") :]
        return url

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"prod", "production"}

    @property
    def access_token_secret(self) -> str:
        return (self.jwt_secret or self.secret_key).strip()

    @property
    def refresh_token_secret(self) -> str:
        return (self.jwt_refresh_secret or self.refresh_secret_key).strip()

    def validate_runtime_configuration(self) -> list[str]:
        issues: list[str] = []
        upload_provider = (self.upload_provider or "").strip().lower()
        otp_provider = (self.otp_provider or "").strip().lower()

        if not self.is_production:
            return issues

        if self.normalized_database_url.startswith("sqlite"):
            issues.append("DATABASE_URL must point to Neon Postgres in production")
        if self.access_token_secret in {"", "change-me"}:
            issues.append("JWT_SECRET or SECRET_KEY must be set to a strong random value")
        if self.refresh_token_secret in {"", "change-me-refresh"}:
            issues.append("JWT_REFRESH_SECRET or REFRESH_SECRET_KEY must be set to a strong random value")
        if otp_provider == "console" and self.otp_allow_debug_code:
            issues.append("Disable OTP_ALLOW_DEBUG_CODE in production or configure a real OTP provider")
        if upload_provider == "cloudinary":
            required = [
                (self.cloudinary_cloud_name or "").strip(),
                (self.cloudinary_api_key or "").strip(),
                (self.cloudinary_api_secret or "").strip(),
            ]
            if not all(required):
                issues.append("Cloudinary credentials are required when UPLOAD_PROVIDER=cloudinary")
        return issues


settings = Settings()
