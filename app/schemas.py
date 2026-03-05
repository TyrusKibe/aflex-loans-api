from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from .phone import is_valid_kenyan_phone, normalize_phone_number


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class MessageResponse(BaseModel):
    message: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=4096)


def _normalize_phone(value: str) -> str:
    return normalize_phone_number(value)


class UserRegisterRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=24)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=3, max_length=160)

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        normalized = _normalize_phone(value)
        if not is_valid_kenyan_phone(normalized):
            raise ValueError("Invalid Kenyan phone number")
        return normalized

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class AuthOtpRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=24)
    purpose: Literal["register", "login"] = "register"
    channel: Literal["sms", "whatsapp", "voice", "email"] = "sms"
    channels: list[Literal["sms", "whatsapp", "voice", "email"]] | None = None
    email: EmailStr | None = None

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        normalized = _normalize_phone(value)
        if not is_valid_kenyan_phone(normalized):
            raise ValueError("Invalid Kenyan phone number")
        return normalized

    @field_validator("channels")
    @classmethod
    def normalize_channels(
        cls, value: list[Literal["sms", "whatsapp", "voice", "email"]] | None
    ) -> list[Literal["sms", "whatsapp", "voice", "email"]] | None:
        if not value:
            return value
        seen: set[str] = set()
        normalized: list[Literal["sms", "whatsapp", "voice", "email"]] = []
        for item in value:
            if item not in seen:
                seen.add(item)
                normalized.append(item)
        return normalized


class AuthOtpRequestOut(BaseModel):
    challenge_id: str
    phone: str
    purpose: Literal["register", "login"]
    channel: Literal["sms", "whatsapp", "voice", "email"]
    channels: list[Literal["sms", "whatsapp", "voice", "email"]] = Field(default_factory=lambda: ["sms"])
    email: EmailStr | None = None
    expires_in_seconds: int
    retry_after_seconds: int
    message: str
    delivery_results: dict[str, str] = Field(default_factory=dict)
    debug_code: str | None = None


class AuthOtpVerifyRequest(BaseModel):
    challenge_id: str = Field(min_length=12, max_length=128)
    otp_code: str = Field(min_length=4, max_length=8)

    @field_validator("otp_code")
    @classmethod
    def normalize_otp_code(cls, value: str) -> str:
        digits = "".join(char for char in value if char.isdigit())
        if len(digits) < 4:
            raise ValueError("OTP must have at least 4 digits")
        return digits


class AuthOtpVerifyOut(BaseModel):
    challenge_id: str
    verified: bool
    phone: str
    purpose: Literal["register", "login"]
    verified_at: datetime


class UserLoginRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=24)
    password: str

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        normalized_input = value.strip().lower()
        if "@" in normalized_input:
            return normalized_input
        normalized_phone = _normalize_phone(value)
        if not is_valid_kenyan_phone(normalized_phone):
            raise ValueError("Invalid Kenyan phone number")
        return normalized_phone


class UserOut(BaseModel):
    id: str
    email: str | None = None
    phone: str | None = None
    full_name: str
    role: Literal["borrower", "admin"]
    created_at: datetime

    class Config:
        from_attributes = True


class BorrowerProfileUpsert(BaseModel):
    national_id: str = Field(min_length=6, max_length=20)
    phone: str = Field(min_length=10, max_length=20)
    notification_email: EmailStr | None = None
    date_of_birth: str = Field(min_length=8, max_length=20)
    county: str = Field(min_length=2, max_length=60)
    sub_county: str = Field(min_length=2, max_length=60)
    gps_lat: float | None = Field(default=None, ge=-90, le=90)
    gps_lng: float | None = Field(default=None, ge=-180, le=180)
    location_accuracy_m: float | None = Field(default=None, ge=0, le=10000)
    location_captured_at: datetime | None = None
    location_landmark: str | None = Field(default=None, min_length=4, max_length=200)
    employment_status: str = Field(default="unknown", max_length=60)
    monthly_income: float = Field(ge=0)
    mpesa_monthly_inflow: float = Field(ge=0)
    kra_pin: str | None = Field(default=None, min_length=11, max_length=20)
    mpesa_phone: str | None = Field(default=None, min_length=10, max_length=20)
    residential_address: str | None = Field(default=None, min_length=4, max_length=200)
    next_of_kin_name: str | None = Field(default=None, min_length=3, max_length=160)
    next_of_kin_phone: str | None = Field(default=None, min_length=10, max_length=20)
    id_front_hash: str | None = Field(default=None, min_length=16, max_length=128)
    id_back_hash: str | None = Field(default=None, min_length=16, max_length=128)
    selfie_image_hash: str | None = Field(default=None, min_length=16, max_length=128)
    selfie_liveness_score: float | None = Field(default=None, ge=0, le=1)
    is_selfie_verified: bool = False
    business_name: str | None = Field(default=None, max_length=200)
    business_age_months: int | None = Field(default=None, ge=0, le=600)
    business_photo_hash: str | None = Field(default=None, max_length=128)
    payment_proof_type: Literal["mpesa_statement", "bank_statement"] | None = None
    payment_proof_hash: str | None = Field(default=None, min_length=16, max_length=128)
    device_fingerprint: str | None = Field(default=None, max_length=128)
    is_id_verified: bool = False
    is_location_verified: bool = False

    @field_validator("national_id")
    @classmethod
    def validate_national_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.isdigit():
            raise ValueError("National ID must be numeric")
        if len(normalized) < 6 or len(normalized) > 12:
            raise ValueError("National ID length is invalid")
        return normalized

    @field_validator("phone", "mpesa_phone", "next_of_kin_phone")
    @classmethod
    def normalize_kyc_phone(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        normalized = _normalize_phone(value)
        if not is_valid_kenyan_phone(normalized):
            raise ValueError("Invalid Kenyan phone number")
        return normalized

    @field_validator("kra_pin")
    @classmethod
    def validate_kra_pin(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        normalized = value.strip().upper().replace(" ", "")
        if len(normalized) != 11:
            raise ValueError("KRA PIN must be 11 characters")
        if not normalized[0].isalpha() or not normalized[-1].isalpha():
            raise ValueError("KRA PIN format is invalid")
        if not normalized[1:10].isdigit():
            raise ValueError("KRA PIN format is invalid")
        return normalized

    @field_validator("date_of_birth")
    @classmethod
    def validate_date_of_birth(cls, value: str) -> str:
        try:
            dob = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as error:
            raise ValueError("Date of birth must be YYYY-MM-DD") from error
        today = datetime.now(UTC).date()
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        if age < 18:
            raise ValueError("Borrower must be at least 18 years old")
        return value

    @field_validator("location_landmark")
    @classmethod
    def normalize_location_landmark(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_location_payload(self) -> "BorrowerProfileUpsert":
        has_lat = self.gps_lat is not None
        has_lng = self.gps_lng is not None
        if has_lat != has_lng:
            raise ValueError("Both gps_lat and gps_lng are required together")
        if self.is_location_verified and not (has_lat and has_lng):
            raise ValueError("Location verification requires gps coordinates")
        if self.is_location_verified and self.location_captured_at is None:
            raise ValueError("Location verification requires capture timestamp")
        if self.is_location_verified and (self.location_landmark is None or self.location_landmark.strip() == ""):
            raise ValueError("Location verification requires landmark description")
        if self.location_accuracy_m is not None and not (has_lat and has_lng):
            raise ValueError("Location accuracy requires gps coordinates")
        if self.location_captured_at is not None and not (has_lat and has_lng):
            raise ValueError("Location timestamp requires gps coordinates")
        if self.payment_proof_hash and not self.payment_proof_type:
            raise ValueError("Payment proof type is required when payment proof is provided")
        if self.payment_proof_type and not self.payment_proof_hash:
            raise ValueError("Payment proof file is required when payment proof type is selected")
        return self


class BorrowerProfileOut(BorrowerProfileUpsert):
    id: str
    user_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ConsentRequest(BaseModel):
    consent_type: Literal[
        "privacy_policy",
        "loan_terms",
        "data_processing",
        "credit_assessment",
        "collection_policy",
    ]
    accepted: bool = True


class ConsentOut(BaseModel):
    id: str
    user_id: str
    consent_type: str
    accepted: bool
    accepted_at: datetime

    class Config:
        from_attributes = True


class LoanProductRule(BaseModel):
    product_type: Literal["personal", "business"]
    amount_min: int
    amount_max: int
    term_days_options: list[int]
    fixed_markup_amount: float
    upfront_fee_rate: float = 0.0
    daily_late_interest_rate: float = 0.0
    monthly_interest_min: float
    monthly_interest_max: float
    processing_fee_min: float
    processing_fee_max: float
    late_fee_amount: float


class LoanApplicationCreate(BaseModel):
    product_type: Literal["personal", "business"]
    requested_amount: float = Field(gt=0)
    term_days: int = Field(gt=0)
    purpose: str = Field(min_length=4, max_length=200)

    @field_validator("purpose")
    @classmethod
    def normalize_purpose(cls, value: str) -> str:
        return value.strip()


class LoanApplicationOut(BaseModel):
    id: str
    user_id: str
    product_type: Literal["personal", "business"]
    requested_amount: float
    disbursement_amount: float
    fixed_markup_amount: float
    term_days: int
    purpose: str
    status: str
    risk_score: int | None = None
    risk_band: str | None = None
    recommended_limit: float | None = None
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class RiskAssessmentOut(BaseModel):
    application_id: str
    approved: bool
    risk_score: int
    risk_band: str
    max_offer_amount: float
    pricing_multiplier: float
    reasons: list[str]


class LoanOfferOut(BaseModel):
    id: str
    application_id: str
    principal_amount: float
    disbursement_amount: float
    fixed_markup_amount: float
    term_days: int
    monthly_interest_rate: float
    processing_fee_rate: float
    processing_fee_amount: float
    interest_amount: float
    late_fee_amount: float
    total_due: float
    duplum_cap_amount: float
    status: str
    expires_at: datetime
    created_at: datetime

    class Config:
        from_attributes = True


class LoanOut(BaseModel):
    id: str
    user_id: str
    application_id: str
    offer_id: str
    product_type: Literal["personal", "business"]
    principal_amount: float
    disbursement_amount: float
    fixed_markup_amount: float
    term_days: int
    monthly_interest_rate: float
    processing_fee_amount: float
    interest_amount: float
    late_fee_amount: float
    total_due: float
    outstanding_amount: float
    duplum_cap_amount: float
    status: str
    disbursed_at: datetime | None = None
    due_at: datetime
    closed_at: datetime | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class RepaymentRequest(BaseModel):
    amount: float = Field(gt=0)
    channel: Literal["mpesa", "bank", "card", "cash"] = "mpesa"
    paid_to_phone: str | None = Field(default=None, min_length=7, max_length=24)
    reference: str = Field(min_length=4, max_length=100)

    @field_validator("paid_to_phone")
    @classmethod
    def normalize_paid_to_phone(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        normalized = _normalize_phone(value)
        if not is_valid_kenyan_phone(normalized):
            raise ValueError("Invalid Kenyan M-Pesa collection number")
        return normalized


class RepaymentOut(BaseModel):
    id: str
    loan_id: str
    amount: float
    channel: str
    paid_to_phone: str | None = None
    reference: str
    created_at: datetime

    class Config:
        from_attributes = True


class UploadPhotoOut(BaseModel):
    id: str
    user_id: str
    loan_id: str | None = None
    photo_url: str
    doc_type: str
    provider: str
    created_at: datetime

    class Config:
        from_attributes = True


class LocationOut(BaseModel):
    id: str
    user_id: str
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    captured_at: datetime

    class Config:
        from_attributes = True


class CreateLoanRequest(BaseModel):
    product_type: Literal["personal", "business"] = "personal"
    amount: float = Field(gt=0)
    term_days: int = Field(gt=0)
    purpose: str = Field(min_length=4, max_length=200)


class ApproveLoanRequest(BaseModel):
    application_id: str = Field(min_length=8, max_length=64)


class RepayLoanRequestV2(RepaymentRequest):
    loan_id: str = Field(min_length=8, max_length=64)


class ComplianceSummary(BaseModel):
    total_users: int
    total_applications: int
    approved_applications: int
    active_loans: int
    defaulted_loans: int
    open_fraud_flags: int
    disclaimer: str


class FraudFlagOut(BaseModel):
    id: str
    user_id: str
    application_id: str | None = None
    severity: str
    reason: str
    status: str
    created_at: datetime
    resolved_at: datetime | None = None

    class Config:
        from_attributes = True


class AdminLoanApplicationOut(BaseModel):
    id: str
    user_id: str
    borrower_name: str
    borrower_phone: str | None = None
    product_type: Literal["personal", "business"]
    requested_amount: float
    disbursement_amount: float
    fixed_markup_amount: float
    term_days: int
    purpose: str
    status: str
    risk_score: int | None = None
    risk_band: str | None = None
    recommended_limit: float | None = None
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime
