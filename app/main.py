from datetime import UTC, datetime, timedelta
import base64
from decimal import Decimal
import html
import json
import logging
import mimetypes
import random
import secrets
import smtplib
import threading
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import func, inspect, text
from sqlalchemy.orm import Session

from .audit import write_audit_log
from .config import settings
from .database import SessionLocal, engine, get_db
from .deps import get_current_user, require_admin
from .models import (
    ApplicationStatus,
    BorrowerProfile,
    Consent,
    DocumentPhoto,
    FraudFlag,
    FraudSeverity,
    FraudStatus,
    Loan,
    LoanApplication,
    LoanOffer,
    LoanStatus,
    LocationRecord,
    OfferStatus,
    ProductType,
    Repayment,
    User,
    UserRole,
)
from .pricing import compute_pricing
from .risk import compute_risk_score
from .schemas import (
    AdminLoanApplicationOut,
    AuthOtpRequest,
    AuthOtpRequestOut,
    AuthOtpVerifyOut,
    AuthOtpVerifyRequest,
    ApproveLoanRequest,
    BorrowerProfileOut,
    BorrowerProfileUpsert,
    ComplianceSummary,
    ConsentOut,
    ConsentRequest,
    CreateLoanRequest,
    FraudFlagOut,
    LoanApplicationCreate,
    LoanApplicationOut,
    LoanOfferOut,
    LoanOut,
    LocationOut,
    LoanProductRule,
    MessageResponse,
    RefreshTokenRequest,
    RepaymentOut,
    RepaymentRequest,
    RepayLoanRequestV2,
    RiskAssessmentOut,
    Token,
    UploadPhotoOut,
    UserLoginRequest,
    UserOut,
    UserRegisterRequest,
)
from .security import create_access_token, create_refresh_token, decode_refresh_token, hash_password, verify_password
from .phone import is_valid_kenyan_phone, normalize_phone_number
from .storage import upload_photo_bytes

REQUIRED_CONSENTS = {
    "privacy_policy",
    "loan_terms",
    "data_processing",
    "credit_assessment",
    "collection_policy",
}
OTP_LENGTH = 6
OTP_EXPIRY_SECONDS = 5 * 60
OTP_RESEND_SECONDS = 45
OTP_MAX_ATTEMPTS = 5
OTP_CHALLENGES: dict[str, dict[str, object]] = {}
OTP_LAST_SENT_AT: dict[str, datetime] = {}
RATE_LIMIT_BUCKETS: dict[str, list[float]] = {}
RATE_LIMIT_LOCK = threading.Lock()
_REMINDER_LOCK = threading.Lock()
_REMINDER_WORKER_STARTED = False

logger = logging.getLogger("aflex.api")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

app = FastAPI(title=settings.app_name, version="1.0.0")

allow_all_origins = "*" in settings.cors_origins_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if allow_all_origins else settings.cors_origins_list,
    allow_credentials=not allow_all_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _request_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _is_rate_limited(bucket: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    now = time.time()
    with RATE_LIMIT_LOCK:
        entries = RATE_LIMIT_BUCKETS.get(bucket, [])
        entries = [ts for ts in entries if now - ts < window_seconds]
        if len(entries) >= limit:
            retry_after = max(1, int(window_seconds - (now - entries[0])))
            RATE_LIMIT_BUCKETS[bucket] = entries
            return True, retry_after
        entries.append(now)
        RATE_LIMIT_BUCKETS[bucket] = entries
        return False, 0


@app.middleware("http")
async def request_logging_and_error_middleware(request: Request, call_next):
    start = time.perf_counter()
    request_id = secrets.token_hex(8)
    try:
        path = request.url.path
        rate_targets = {
            f"{settings.api_v1_prefix}/auth/login": settings.login_rate_limit_per_minute,
            f"{settings.api_v1_prefix}/auth/otp/request": settings.otp_rate_limit_per_minute,
            f"{settings.api_v1_prefix}/auth/otp/verify": settings.otp_rate_limit_per_minute,
        }
        if request.method == "POST" and path in rate_targets:
            limited, retry_after = _is_rate_limited(
                bucket=f"{path}:{_request_client_ip(request)}",
                limit=max(1, rate_targets[path]),
                window_seconds=60,
            )
            if limited:
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                    content={"detail": "Too many requests. Please retry shortly."},
                )

        response = await call_next(request)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled request error request_id=%s path=%s", request_id, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error", "request_id": request_id})

    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"
    logger.info(
        "%s %s status=%s ip=%s duration_ms=%.2f request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        _request_client_ip(request),
        elapsed_ms,
        request_id,
    )
    return response


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_float(value: Decimal | float | int | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _fixed_markup_amount() -> float:
    return max(0.0, round(float(settings.fixed_markup_amount), 2))


def _personal_upfront_fee_rate() -> float:
    return min(0.95, max(0.0, float(settings.personal_upfront_fee_rate)))


def _personal_daily_late_interest_rate() -> float:
    return min(0.2, max(0.0, float(settings.personal_daily_late_interest_rate)))


def _normalize_product_type_value(product_type: ProductType | str) -> str:
    if isinstance(product_type, ProductType):
        return product_type.value
    return f"{product_type}"


def _contract_amount_for_disbursement(disbursement_amount: float, product_type: ProductType | str) -> float:
    disbursement = max(0.0, float(disbursement_amount))
    product_value = _normalize_product_type_value(product_type)
    if product_value == ProductType.personal.value:
        fee_rate = _personal_upfront_fee_rate()
        denominator = 1.0 - fee_rate
        if denominator <= 0:
            return round(disbursement, 2)
        return round(disbursement / denominator, 2)
    return round(disbursement + _fixed_markup_amount(), 2)


def _disbursement_amount_for_contract(contract_amount: float, product_type: ProductType | str) -> float:
    contract = max(0.0, float(contract_amount))
    product_value = _normalize_product_type_value(product_type)
    if product_value == ProductType.personal.value:
        return round(contract * (1.0 - _personal_upfront_fee_rate()), 2)
    return round(contract - _fixed_markup_amount(), 2)


def _upfront_fee_amount_for_contract(contract_amount: float, product_type: ProductType | str) -> float:
    contract = max(0.0, float(contract_amount))
    disbursed = _disbursement_amount_for_contract(contract, product_type)
    return round(max(0.0, contract - disbursed), 2)


def _normalize_phone_number(value: str) -> str:
    return normalize_phone_number(value)


def _normalize_kenyan_phone_or_empty(value: str | None) -> str:
    if not value:
        return ""
    normalized = _normalize_phone_number(value)
    return normalized if is_valid_kenyan_phone(normalized) else ""


def _expected_mpesa_collection_phone() -> str:
    normalized = _normalize_kenyan_phone_or_empty(settings.mpesa_collection_phone)
    if not normalized:
        raise HTTPException(status_code=500, detail="M-Pesa collection phone is not configured correctly")
    return normalized


def _phone_to_internal_email(phone: str) -> str:
    digits = "".join(char for char in phone if char.isdigit())
    return f"user-{digits}@phone.aflex.local"


def _ensure_column(table_name: str, column_name: str, ddl: str) -> None:
    inspector = inspect(engine)
    existing = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name in existing:
        return
    with engine.begin() as connection:
        connection.execute(text(ddl))


def _ensure_schema_compatibility() -> None:
    _ensure_column("users", "phone", "ALTER TABLE users ADD COLUMN phone VARCHAR(20)")

    _ensure_column("borrower_profiles", "kra_pin", "ALTER TABLE borrower_profiles ADD COLUMN kra_pin VARCHAR(20)")
    _ensure_column("borrower_profiles", "mpesa_phone", "ALTER TABLE borrower_profiles ADD COLUMN mpesa_phone VARCHAR(20)")
    _ensure_column("borrower_profiles", "location_accuracy_m", "ALTER TABLE borrower_profiles ADD COLUMN location_accuracy_m FLOAT")
    _ensure_column(
        "borrower_profiles",
        "location_captured_at",
        "ALTER TABLE borrower_profiles ADD COLUMN location_captured_at TIMESTAMP",
    )
    _ensure_column(
        "borrower_profiles",
        "location_landmark",
        "ALTER TABLE borrower_profiles ADD COLUMN location_landmark VARCHAR(200)",
    )
    _ensure_column(
        "borrower_profiles",
        "residential_address",
        "ALTER TABLE borrower_profiles ADD COLUMN residential_address VARCHAR(200)",
    )
    _ensure_column(
        "borrower_profiles",
        "next_of_kin_name",
        "ALTER TABLE borrower_profiles ADD COLUMN next_of_kin_name VARCHAR(160)",
    )
    _ensure_column(
        "borrower_profiles",
        "next_of_kin_phone",
        "ALTER TABLE borrower_profiles ADD COLUMN next_of_kin_phone VARCHAR(20)",
    )
    _ensure_column("borrower_profiles", "id_front_hash", "ALTER TABLE borrower_profiles ADD COLUMN id_front_hash VARCHAR(128)")
    _ensure_column("borrower_profiles", "id_back_hash", "ALTER TABLE borrower_profiles ADD COLUMN id_back_hash VARCHAR(128)")
    _ensure_column(
        "borrower_profiles",
        "selfie_image_hash",
        "ALTER TABLE borrower_profiles ADD COLUMN selfie_image_hash VARCHAR(128)",
    )
    _ensure_column(
        "borrower_profiles",
        "selfie_liveness_score",
        "ALTER TABLE borrower_profiles ADD COLUMN selfie_liveness_score FLOAT",
    )
    _ensure_column(
        "borrower_profiles",
        "is_selfie_verified",
        "ALTER TABLE borrower_profiles ADD COLUMN is_selfie_verified BOOLEAN DEFAULT FALSE",
    )
    _ensure_column(
        "borrower_profiles",
        "payment_proof_type",
        "ALTER TABLE borrower_profiles ADD COLUMN payment_proof_type VARCHAR(40)",
    )
    _ensure_column(
        "borrower_profiles",
        "payment_proof_hash",
        "ALTER TABLE borrower_profiles ADD COLUMN payment_proof_hash VARCHAR(128)",
    )
    _ensure_column(
        "borrower_profiles",
        "notification_email",
        "ALTER TABLE borrower_profiles ADD COLUMN notification_email VARCHAR(255)",
    )
    _ensure_column(
        "loans",
        "reminder_10d_sent_at",
        "ALTER TABLE loans ADD COLUMN reminder_10d_sent_at TIMESTAMP",
    )
    _ensure_column(
        "loans",
        "late_interest_days_applied",
        "ALTER TABLE loans ADD COLUMN late_interest_days_applied INTEGER DEFAULT 0",
    )
    _ensure_column("repayments", "paid_to_phone", "ALTER TABLE repayments ADD COLUMN paid_to_phone VARCHAR(20)")

    with engine.begin() as connection:
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_phone ON users (phone)"))


def _is_local_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


def _prune_otp_state(now: datetime) -> None:
    expired = [
        challenge_id
        for challenge_id, challenge in OTP_CHALLENGES.items()
        if challenge.get("expires_at") and challenge["expires_at"] < now - timedelta(minutes=10)
    ]
    for challenge_id in expired:
        OTP_CHALLENGES.pop(challenge_id, None)

    stale_phones = [
        phone
        for phone, sent_at in OTP_LAST_SENT_AT.items()
        if sent_at < now - timedelta(hours=2)
    ]
    for phone in stale_phones:
        OTP_LAST_SENT_AT.pop(phone, None)


def _mask_phone(value: str) -> str:
    if len(value) <= 4:
        return value
    return f"{value[:4]}{'*' * max(2, len(value) - 7)}{value[-3:]}"


def _send_africastalking_sms(*, phone: str, message: str) -> tuple[bool, str]:
    username = (settings.otp_africastalking_username or "").strip()
    api_key = (settings.otp_africastalking_api_key or "").strip()
    if not username or not api_key:
        return False, "Africa's Talking credentials are missing"

    payload = {
        "username": username,
        "to": phone,
        "message": message,
    }
    sender_id = (settings.otp_sender_id or "").strip()
    if sender_id:
        payload["from"] = sender_id

    request = UrlRequest(
        "https://api.africastalking.com/version1/messaging",
        data=urlencode(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "apiKey": api_key,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=25) as response:
            body = response.read().decode("utf-8")
    except HTTPError as error:
        response_body = error.read().decode("utf-8", errors="ignore")
        return False, f"Africa's Talking HTTP {error.code}: {response_body[:180]}"
    except URLError as error:
        return False, f"Africa's Talking network error: {error.reason}"
    except Exception as error:
        return False, f"Africa's Talking unexpected error: {error}"

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return False, "Africa's Talking returned invalid JSON"

    recipients = parsed.get("SMSMessageData", {}).get("Recipients", [])
    if not recipients:
        return False, "Africa's Talking did not return recipients"

    recipient = recipients[0]
    recipient_status = str(recipient.get("status", "")).lower()
    status_code = int(recipient.get("statusCode", 0) or 0)
    success = status_code in {100, 101, 102} or "success" in recipient_status
    if not success:
        return False, f"Africa's Talking delivery failed: {recipient.get('status', 'unknown')}"

    return True, "SMS sent"


def _send_otp_africastalking(*, phone: str, otp_code: str, channel: str) -> tuple[bool, str]:
    if channel != "sms":
        return False, "Selected OTP channel is not supported. Please use SMS."
    return _send_africastalking_sms(phone=phone, message=_otp_text(otp_code))


def _otp_text(otp_code: str) -> str:
    return (
        f"{settings.app_name}: Your verification code is {otp_code}. "
        "It expires in 5 minutes. Do not share this code."
    )


def _send_twilio_form(path: str, payload: dict[str, str]) -> tuple[bool, str]:
    account_sid = (settings.otp_twilio_account_sid or "").strip()
    auth_token = (settings.otp_twilio_auth_token or "").strip()
    if not account_sid or not auth_token:
        return False, "Twilio credentials are missing"

    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/{path}"
    token = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    request = UrlRequest(
        endpoint,
        data=urlencode(payload).encode("utf-8"),
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=25) as response:
            body = response.read().decode("utf-8")
            status_code = response.status
    except HTTPError as error:
        response_body = error.read().decode("utf-8", errors="ignore")
        return False, f"Twilio HTTP {error.code}: {response_body[:200]}"
    except URLError as error:
        return False, f"Twilio network error: {error.reason}"
    except Exception as error:
        return False, f"Twilio unexpected error: {error}"

    if status_code < 200 or status_code >= 300:
        return False, f"Twilio returned status {status_code}: {body[:180]}"
    return True, body


def _send_otp_twilio(*, phone: str, otp_code: str, channel: str) -> tuple[bool, str]:
    message = _otp_text(otp_code)
    if channel == "sms":
        sender = (settings.otp_twilio_sms_from or "").strip()
        if not sender:
            return False, "Twilio SMS sender is missing"
        sent, info = _send_twilio_form(
            "Messages.json",
            {"To": phone, "From": sender, "Body": message},
        )
        return (True, "OTP sent via SMS") if sent else (False, info)

    if channel == "whatsapp":
        sender = (settings.otp_twilio_whatsapp_from or "").strip()
        if not sender:
            return False, "Twilio WhatsApp sender is missing"
        to_value = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
        from_value = sender if sender.startswith("whatsapp:") else f"whatsapp:{sender}"
        sent, info = _send_twilio_form(
            "Messages.json",
            {"To": to_value, "From": from_value, "Body": message},
        )
        return (True, "OTP sent via WhatsApp") if sent else (False, info)

    if channel == "voice":
        sender = (settings.otp_twilio_voice_from or "").strip()
        if not sender:
            return False, "Twilio voice caller ID is missing"
        spaced_digits = " ".join(otp_code)
        twiml = (
            "<Response>"
            "<Say voice='alice'>"
            f"Your Aflex verification code is {spaced_digits}. I repeat, {spaced_digits}."
            "</Say>"
            "</Response>"
        )
        sent, info = _send_twilio_form(
            "Calls.json",
            {"To": phone, "From": sender, "Twiml": twiml},
        )
        return (True, "OTP sent via voice call") if sent else (False, info)

    return False, "Twilio does not support this OTP channel"


def _send_email_message(*, to_email: str, subject: str, text_body: str) -> tuple[bool, str]:
    host = (settings.otp_smtp_host or "").strip()
    username = (settings.otp_smtp_username or "").strip()
    password = settings.otp_smtp_password or ""
    from_email = (settings.otp_smtp_from_email or "").strip()
    port = int(settings.otp_smtp_port)

    if not host or not from_email:
        return False, "SMTP host/from email is missing"
    if not username or not password:
        return False, "SMTP credentials are missing"

    message = EmailMessage()
    message["From"] = from_email
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(text_body)

    try:
        if settings.otp_smtp_use_tls:
            with smtplib.SMTP(host, port, timeout=25) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(username, password)
                refused = smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=25) as smtp:
                smtp.login(username, password)
                refused = smtp.send_message(message)
    except Exception as error:
        return False, f"SMTP delivery failed: {error}"

    if refused:
        rejected = ", ".join(refused.keys())
        return False, f"SMTP recipient rejected: {rejected}"

    return True, "Email accepted by SMTP relay"


def _send_otp_email(*, email: str, otp_code: str) -> tuple[bool, str]:
    return _send_email_message(
        to_email=email,
        subject=f"{settings.app_name} verification code",
        text_body=(
            "Your verification code is "
            f"{otp_code}. It expires in 5 minutes. Do not share this code."
        ),
    )


def _send_otp(*, phone: str, otp_code: str, channel: str, email: str | None = None) -> tuple[bool, str, str]:
    provider = settings.otp_provider.strip().lower()
    if channel in {"whatsapp", "voice"}:
        sent, info = _send_otp_twilio(phone=phone, otp_code=otp_code, channel=channel)
        return sent, info, "twilio"

    if channel == "email":
        if not email:
            return False, "Email is required for email OTP delivery", "smtp"
        sent, info = _send_otp_email(email=email, otp_code=otp_code)
        return sent, info, "smtp"

    if provider == "africastalking":
        sent, info = _send_otp_africastalking(phone=phone, otp_code=otp_code, channel=channel)
        return sent, info, "africastalking"
    if provider == "twilio":
        sent, info = _send_otp_twilio(phone=phone, otp_code=otp_code, channel=channel)
        return sent, info, "twilio"
    if provider == "console":
        if channel != "sms":
            return False, "Selected OTP channel is not supported. Please use SMS.", "console"
        if settings.otp_require_real_delivery:
            return False, "Real OTP delivery is required; configure OTP_PROVIDER=twilio or africastalking", "console"
        if not settings.otp_allow_debug_code:
            return False, "OTP debug is disabled in console mode; configure a real OTP provider", "console"
        return True, f"OTP generated in console mode for {_mask_phone(phone)}", "console"
    return False, f"Unsupported OTP provider '{settings.otp_provider}'", provider


def _send_sms_notification(*, phone: str, message: str) -> tuple[bool, str, str]:
    normalized_phone = _normalize_kenyan_phone_or_empty(phone)
    if not normalized_phone:
        return False, "Invalid borrower phone number", "none"

    has_africastalking = all(
        [
            (settings.otp_africastalking_username or "").strip(),
            (settings.otp_africastalking_api_key or "").strip(),
        ]
    )
    has_twilio = all(
        [
            (settings.otp_twilio_account_sid or "").strip(),
            (settings.otp_twilio_auth_token or "").strip(),
            (settings.otp_twilio_sms_from or "").strip(),
        ]
    )
    if has_twilio:
        sent, info = _send_twilio_form(
            "Messages.json",
            {"To": normalized_phone, "From": (settings.otp_twilio_sms_from or "").strip(), "Body": message},
        )
        if sent:
            return True, "SMS sent via Twilio", "twilio"
        if not has_africastalking:
            return False, info, "twilio"

    if has_africastalking:
        sent, info = _send_africastalking_sms(phone=normalized_phone, message=message)
        return (True, "SMS sent via Africa's Talking", "africastalking") if sent else (False, info, "africastalking")

    return False, "No SMS provider configured for loan notifications", "none"


def _resolve_borrower_notification_email(*, user: User, profile: BorrowerProfile | None) -> str | None:
    override_email = (settings.loan_user_notification_email_override or "").strip()
    if override_email:
        return override_email
    profile_email = (profile.notification_email if profile else "") or ""
    if profile_email.strip():
        return profile_email.strip()
    user_email = (user.email or "").strip()
    if user_email and not user_email.endswith("@phone.aflex.local"):
        return user_email
    return None


def _notify_admin_new_application(*, user: User, profile: BorrowerProfile, application: LoanApplication) -> dict[str, str]:
    admin_email = (settings.loan_admin_alert_email or "").strip()
    if not admin_email:
        return {"email": "Skipped: LOAN_ADMIN_ALERT_EMAIL is not configured"}

    contract_amount = _to_float(application.requested_amount)
    disbursal = _disbursement_amount_for_contract(contract_amount, application.product_type)
    upfront_fee = _upfront_fee_amount_for_contract(contract_amount, application.product_type)
    subject = f"[{settings.app_name}] New loan application pending admin review"
    body = (
        f"A new {application.product_type.value} loan application needs review.\n\n"
        f"Application ID: {application.id}\n"
        f"Borrower: {user.full_name}\n"
        f"Borrower Phone: {profile.phone}\n"
        f"Disbursement Amount: KSh {disbursal:,.2f}\n"
        f"Contract Amount: KSh {contract_amount:,.2f}\n"
        f"Upfront Fee: KSh {upfront_fee:,.2f}\n"
        f"Term: {application.term_days} days\n"
        f"Purpose: {application.purpose}\n"
        f"Risk: {application.risk_band or '-'} ({application.risk_score if application.risk_score is not None else '-'})\n"
        f"Created At: {application.created_at.isoformat()}\n"
    )
    sent, info = _send_email_message(to_email=admin_email, subject=subject, text_body=body)
    return {"email": info if sent else f"Failed: {info}"}


def _notify_borrower_loan_approved(
    *,
    user: User,
    profile: BorrowerProfile | None,
    loan: Loan,
    application: LoanApplication,
) -> dict[str, str]:
    disbursal = _disbursement_amount_for_contract(_to_float(loan.principal_amount), loan.product_type)
    sms_body = (
        f"{settings.app_name}: Your loan is approved. "
        f"Disbursement KSh {disbursal:,.0f}, term {loan.term_days} days. "
        "Please repay on time to increase your limit."
    )
    sms_sent, sms_info, _ = _send_sms_notification(phone=(user.phone or ""), message=sms_body)

    email_status = "Skipped: borrower email not provided"
    borrower_email = _resolve_borrower_notification_email(user=user, profile=profile)
    if borrower_email:
        email_subject = f"{settings.app_name}: Your loan is approved"
        email_body = (
            f"Hello {user.full_name},\n\n"
            "Your loan has been approved.\n"
            f"Loan ID: {loan.id}\n"
            f"Product: {application.product_type.value}\n"
            f"Disbursement Amount: KSh {disbursal:,.2f}\n"
            f"Contract Amount: KSh {_to_float(loan.principal_amount):,.2f}\n"
            f"Total Due: KSh {_to_float(loan.total_due):,.2f}\n"
            f"Due Date: {_as_utc(loan.due_at).isoformat() if _as_utc(loan.due_at) else '-'}\n\n"
            "Repay on time to unlock a higher limit instantly.\n"
        )
        email_sent, email_info = _send_email_message(
            to_email=borrower_email,
            subject=email_subject,
            text_body=email_body,
        )
        email_status = email_info if email_sent else f"Failed: {email_info}"

    return {
        "sms": sms_info if sms_sent else f"Failed: {sms_info}",
        "email": email_status,
    }


def _notify_borrower_limit_upgrade(*, user: User, profile: BorrowerProfile | None, loan: Loan) -> dict[str, str]:
    sms_body = (
        f"{settings.app_name}: Payment received in full for loan {loan.id}. "
        "Your account is cleared and your next borrowing limit is increased instantly."
    )
    sms_sent, sms_info, _ = _send_sms_notification(phone=(user.phone or ""), message=sms_body)

    email_status = "Skipped: borrower email not provided"
    borrower_email = _resolve_borrower_notification_email(user=user, profile=profile)
    if borrower_email:
        email_sent, email_info = _send_email_message(
            to_email=borrower_email,
            subject=f"{settings.app_name}: Loan fully paid and limit upgraded",
            text_body=(
                f"Hello {user.full_name},\n\n"
                f"We have received full repayment for loan {loan.id}.\n"
                "Your account is now cleared, and your next loan limit has been increased.\n"
                "Open the app to request your next loan.\n"
            ),
        )
        email_status = email_info if email_sent else f"Failed: {email_info}"

    return {
        "sms": sms_info if sms_sent else f"Failed: {sms_info}",
        "email": email_status,
    }


def _notify_borrower_due_soon(
    *,
    user: User,
    profile: BorrowerProfile | None,
    loan: Loan,
    days_remaining: int,
) -> tuple[bool, dict[str, str]]:
    due_at = _as_utc(loan.due_at)
    due_label = due_at.strftime("%d %b %Y") if due_at else "-"
    outstanding = _to_float(loan.outstanding_amount)
    sms_body = (
        f"{settings.app_name} reminder: Loan {loan.id} is due in {days_remaining} days "
        f"(Due {due_label}). Outstanding KSh {outstanding:,.0f}. "
        f"Pay via M-Pesa {settings.mpesa_collection_phone}."
    )
    sms_sent, sms_info, _ = _send_sms_notification(phone=(user.phone or ""), message=sms_body)

    email_sent = False
    email_status = "Skipped: borrower email not provided"
    borrower_email = _resolve_borrower_notification_email(user=user, profile=profile)
    if borrower_email:
        email_subject = f"{settings.app_name}: 10-day repayment reminder"
        email_body = (
            f"Hello {user.full_name},\n\n"
            f"This is your repayment reminder for loan {loan.id}.\n"
            f"Outstanding Amount: KSh {outstanding:,.2f}\n"
            f"Due Date: {due_label}\n"
            f"Days Remaining: {days_remaining}\n"
            f"Paybill/Collection Number: {settings.mpesa_collection_phone}\n\n"
            "Please repay on time to keep your profile strong and unlock higher limits.\n"
        )
        email_sent, email_info = _send_email_message(
            to_email=borrower_email,
            subject=email_subject,
            text_body=email_body,
        )
        email_status = email_info if email_sent else f"Failed: {email_info}"

    delivered = sms_sent or email_sent
    return delivered, {
        "sms": sms_info if sms_sent else f"Failed: {sms_info}",
        "email": email_status,
    }


def _due_reminder_days_before() -> int:
    return max(1, int(settings.due_reminder_days_before))


def _due_reminder_interval_seconds() -> int:
    return max(300, int(settings.due_reminder_scan_interval_minutes) * 60)


def _execute_due_soon_reminders(db: Session, *, trigger: str) -> dict[str, int]:
    now = _utc_now()
    due_before_days = _due_reminder_days_before()
    due_cutoff = now + timedelta(days=due_before_days)
    loans = (
        db.query(Loan)
        .filter(
            Loan.status == LoanStatus.active,
            Loan.outstanding_amount > 0,
            Loan.due_at > now,
            Loan.due_at <= due_cutoff,
            Loan.reminder_10d_sent_at.is_(None),
        )
        .order_by(Loan.due_at.asc())
        .limit(500)
        .all()
    )

    scanned = len(loans)
    sent = 0
    failed = 0
    for loan in loans:
        borrower = db.query(User).filter(User.id == loan.user_id).first()
        if not borrower:
            failed += 1
            continue
        profile = db.query(BorrowerProfile).filter(BorrowerProfile.user_id == borrower.id).first()
        due_at = _as_utc(loan.due_at)
        days_remaining = 1
        if due_at:
            delta_seconds = max(0, int((due_at - now).total_seconds()))
            days_remaining = max(1, int((delta_seconds + 86399) // 86400))

        delivered, notify_result = _notify_borrower_due_soon(
            user=borrower,
            profile=profile,
            loan=loan,
            days_remaining=days_remaining,
        )
        metadata = {
            **notify_result,
            "trigger": trigger,
            "loan_status": loan.status.value,
            "due_days_remaining": days_remaining,
        }
        if delivered:
            sent += 1
            loan.reminder_10d_sent_at = now
            write_audit_log(
                db,
                action="loan.due_10d_reminder",
                entity_type="loan",
                entity_id=loan.id,
                user_id=borrower.id,
                metadata=metadata,
            )
        else:
            failed += 1
            write_audit_log(
                db,
                action="loan.due_10d_reminder.failed",
                entity_type="loan",
                entity_id=loan.id,
                user_id=borrower.id,
                metadata=metadata,
            )
    db.commit()
    return {"scanned": scanned, "sent": sent, "failed": failed}


def _run_due_soon_reminders_once(*, trigger: str) -> dict[str, int]:
    if not settings.due_reminder_enabled:
        return {"scanned": 0, "sent": 0, "failed": 0}

    if not _REMINDER_LOCK.acquire(blocking=False):
        return {"scanned": 0, "sent": 0, "failed": 0}
    try:
        db = SessionLocal()
        try:
            return _execute_due_soon_reminders(db, trigger=trigger)
        finally:
            db.close()
    finally:
        _REMINDER_LOCK.release()


def _due_reminder_worker() -> None:
    while True:
        try:
            _run_due_soon_reminders_once(trigger="scheduler")
        except Exception:
            # Keep worker alive; failures are logged per-attempt in audit where possible.
            pass
        time.sleep(_due_reminder_interval_seconds())


def _count_repaid_loans(db: Session, user_id: str) -> int:
    return (
        db.query(func.count(Loan.id))
        .filter(Loan.user_id == user_id, Loan.status == LoanStatus.repaid)
        .scalar()
        or 0
    )


def _daily_late_interest_rate_for_product(product_type: ProductType | str) -> float:
    product_value = _normalize_product_type_value(product_type)
    if product_value == ProductType.personal.value:
        return _personal_daily_late_interest_rate()
    return 0.0


def _apply_late_interest_if_due(loan: Loan, *, now: datetime | None = None) -> bool:
    # Accrue only once per overdue day to keep totals stable across repeated reads.
    if loan.status != LoanStatus.active:
        return False
    due_at = _as_utc(loan.due_at)
    if due_at is None:
        return False
    current_time = now or _utc_now()
    overdue_seconds = (current_time - due_at).total_seconds()
    if overdue_seconds <= 0:
        return False

    daily_rate = _daily_late_interest_rate_for_product(loan.product_type)
    if daily_rate <= 0:
        return False

    overdue_days = int(overdue_seconds // 86400)
    if overdue_days <= 0:
        return False

    applied_days = max(0, int(loan.late_interest_days_applied or 0))
    if overdue_days <= applied_days:
        return False

    days_to_apply = overdue_days - applied_days
    principal = _to_float(loan.principal_amount)
    if principal <= 0:
        loan.late_interest_days_applied = overdue_days
        return True

    incremental_interest = round(principal * daily_rate * days_to_apply, 2)
    current_total_due = _to_float(loan.total_due)
    current_outstanding = _to_float(loan.outstanding_amount)
    current_late_total = _to_float(loan.late_fee_amount)
    duplum_cap = _to_float(loan.duplum_cap_amount) or round(principal * 2.0, 2)
    headroom = max(0.0, round(duplum_cap - current_total_due, 2))
    applied_interest = min(incremental_interest, headroom)

    if applied_interest > 0:
        loan.total_due = round(current_total_due + applied_interest, 2)
        loan.outstanding_amount = round(current_outstanding + applied_interest, 2)
        loan.late_fee_amount = round(current_late_total + applied_interest, 2)
    loan.late_interest_days_applied = overdue_days
    return True


def _product_rules() -> list[LoanProductRule]:
    return [
        LoanProductRule(
            product_type="personal",
            amount_min=settings.personal_min_amount,
            amount_max=settings.personal_max_amount,
            term_days_options=[settings.personal_term_days],
            fixed_markup_amount=_fixed_markup_amount(),
            upfront_fee_rate=_personal_upfront_fee_rate(),
            daily_late_interest_rate=_personal_daily_late_interest_rate(),
            monthly_interest_min=settings.personal_monthly_interest_min,
            monthly_interest_max=settings.personal_monthly_interest_max,
            processing_fee_min=settings.processing_fee_min,
            processing_fee_max=settings.processing_fee_max,
            late_fee_amount=settings.personal_late_fee,
        ),
    ]


def _validate_product_input(*, product_type: str, term_days: int, disbursement_amount: float) -> None:
    if product_type != "personal":
        raise HTTPException(status_code=422, detail="Aflex currently issues personal loans only")
    if term_days != settings.personal_term_days:
        raise HTTPException(status_code=422, detail="Personal loans must be 30 days")
    if disbursement_amount < settings.personal_min_amount or disbursement_amount > settings.personal_max_amount:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Personal loan disbursement amount must be between "
                f"{settings.personal_min_amount} and {settings.personal_max_amount}"
            ),
        )


def _ensure_required_consents(db: Session, user_id: str) -> None:
    if not settings.require_explicit_consent:
        return
    consent_rows = (
        db.query(Consent)
        .filter(Consent.user_id == user_id, Consent.accepted.is_(True))
        .all()
    )
    types = {row.consent_type for row in consent_rows}
    missing = REQUIRED_CONSENTS - types
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required consents: {', '.join(sorted(missing))}",
        )


def _active_loans_count(db: Session, user_id: str) -> int:
    return (
        db.query(Loan)
        .filter(
            Loan.user_id == user_id,
            Loan.status.in_([LoanStatus.active, LoanStatus.pending_disbursement]),
        )
        .count()
    )


def _daily_applications_count(db: Session, user_id: str) -> int:
    now = _utc_now()
    start_day = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return (
        db.query(LoanApplication)
        .filter(LoanApplication.user_id == user_id, LoanApplication.created_at >= start_day)
        .count()
    )


def _flag_if_fraud_signals(db: Session, profile: BorrowerProfile, application_id: str | None = None) -> list[FraudFlag]:
    flags: list[FraudFlag] = []
    if profile.device_fingerprint:
        duplicate_fp = (
            db.query(BorrowerProfile)
            .filter(
                BorrowerProfile.device_fingerprint == profile.device_fingerprint,
                BorrowerProfile.user_id != profile.user_id,
            )
            .count()
        )
        if duplicate_fp > 0:
            flags.append(
                FraudFlag(
                    user_id=profile.user_id,
                    application_id=application_id,
                    severity=FraudSeverity.high,
                    reason="Device fingerprint reused by multiple accounts",
                )
            )

    if profile.business_photo_hash:
        duplicate_photo = (
            db.query(BorrowerProfile)
            .filter(
                BorrowerProfile.business_photo_hash == profile.business_photo_hash,
                BorrowerProfile.user_id != profile.user_id,
            )
            .count()
        )
        if duplicate_photo > 0:
            flags.append(
                FraudFlag(
                    user_id=profile.user_id,
                    application_id=application_id,
                    severity=FraudSeverity.medium,
                    reason="Identity image hash matches existing account",
                )
            )

    if profile.selfie_image_hash:
        duplicate_selfie = (
            db.query(BorrowerProfile)
            .filter(
                BorrowerProfile.selfie_image_hash == profile.selfie_image_hash,
                BorrowerProfile.user_id != profile.user_id,
            )
            .count()
        )
        if duplicate_selfie > 0:
            flags.append(
                FraudFlag(
                    user_id=profile.user_id,
                    application_id=application_id,
                    severity=FraudSeverity.critical,
                    reason="Selfie hash matches another account",
                )
            )

    if profile.id_front_hash:
        duplicate_id_front = (
            db.query(BorrowerProfile)
            .filter(
                BorrowerProfile.id_front_hash == profile.id_front_hash,
                BorrowerProfile.user_id != profile.user_id,
            )
            .count()
        )
        if duplicate_id_front > 0:
            flags.append(
                FraudFlag(
                    user_id=profile.user_id,
                    application_id=application_id,
                    severity=FraudSeverity.high,
                    reason="Front ID hash matches another account",
                )
            )

    for flag in flags:
        db.add(flag)
    return flags


def _risk_assessment_for(
    *,
    db: Session,
    profile: BorrowerProfile,
    application: LoanApplication,
) -> RiskAssessmentOut:
    has_active = _active_loans_count(db, profile.user_id) > 0
    has_open_fraud = (
        db.query(FraudFlag)
        .filter(FraudFlag.user_id == profile.user_id, FraudFlag.status == FraudStatus.open)
        .count()
        > 0
    )
    result = compute_risk_score(
        product_type=application.product_type,
        requested_amount=_to_float(application.requested_amount),
        monthly_income=_to_float(profile.monthly_income),
        mpesa_monthly_inflow=_to_float(profile.mpesa_monthly_inflow),
        business_age_months=profile.business_age_months,
        is_id_verified=profile.is_id_verified,
        is_location_verified=profile.is_location_verified,
        has_active_loan=has_active,
        has_open_fraud_flags=has_open_fraud,
    )
    return RiskAssessmentOut(
        application_id=application.id,
        approved=result.approved,
        risk_score=result.score,
        risk_band=result.band,
        max_offer_amount=result.max_offer_amount,
        pricing_multiplier=result.pricing_multiplier,
        reasons=result.reasons,
    )


def _serialize_application(row: LoanApplication) -> LoanApplicationOut:
    contract_amount = _to_float(row.requested_amount)
    fixed_markup = _upfront_fee_amount_for_contract(contract_amount, row.product_type)
    return LoanApplicationOut(
        id=row.id,
        user_id=row.user_id,
        product_type=row.product_type.value,
        requested_amount=contract_amount,
        disbursement_amount=_disbursement_amount_for_contract(contract_amount, row.product_type),
        fixed_markup_amount=fixed_markup,
        term_days=row.term_days,
        purpose=row.purpose,
        status=row.status.value,
        risk_score=row.risk_score,
        risk_band=row.risk_band,
        recommended_limit=_to_float(row.recommended_limit) if row.recommended_limit is not None else None,
        rejection_reason=row.rejection_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _serialize_admin_application(
    *,
    row: LoanApplication,
    borrower: User,
    profile: BorrowerProfile | None,
) -> AdminLoanApplicationOut:
    contract_amount = _to_float(row.requested_amount)
    fixed_markup = _upfront_fee_amount_for_contract(contract_amount, row.product_type)
    borrower_phone = borrower.phone or (profile.phone if profile else None)
    return AdminLoanApplicationOut(
        id=row.id,
        user_id=row.user_id,
        borrower_name=borrower.full_name,
        borrower_phone=borrower_phone,
        product_type=row.product_type.value,
        requested_amount=contract_amount,
        disbursement_amount=_disbursement_amount_for_contract(contract_amount, row.product_type),
        fixed_markup_amount=fixed_markup,
        term_days=row.term_days,
        purpose=row.purpose,
        status=row.status.value,
        risk_score=row.risk_score,
        risk_band=row.risk_band,
        recommended_limit=_to_float(row.recommended_limit) if row.recommended_limit is not None else None,
        rejection_reason=row.rejection_reason,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _serialize_offer(row: LoanOffer) -> LoanOfferOut:
    principal = _to_float(row.principal_amount)
    product_type = row.application.product_type if row.application else ProductType.personal
    fixed_markup = _upfront_fee_amount_for_contract(principal, product_type)
    return LoanOfferOut(
        id=row.id,
        application_id=row.application_id,
        principal_amount=principal,
        disbursement_amount=_disbursement_amount_for_contract(principal, product_type),
        fixed_markup_amount=fixed_markup,
        term_days=row.term_days,
        monthly_interest_rate=_to_float(row.monthly_interest_rate),
        processing_fee_rate=_to_float(row.processing_fee_rate),
        processing_fee_amount=_to_float(row.processing_fee_amount),
        interest_amount=_to_float(row.interest_amount),
        late_fee_amount=_to_float(row.late_fee_amount),
        total_due=_to_float(row.total_due),
        duplum_cap_amount=_to_float(row.duplum_cap_amount),
        status=row.status.value,
        expires_at=_as_utc(row.expires_at),
        created_at=_as_utc(row.created_at),
    )


def _serialize_loan(row: Loan) -> LoanOut:
    principal = _to_float(row.principal_amount)
    fixed_markup = _upfront_fee_amount_for_contract(principal, row.product_type)
    return LoanOut(
        id=row.id,
        user_id=row.user_id,
        application_id=row.application_id,
        offer_id=row.offer_id,
        product_type=row.product_type.value,
        principal_amount=principal,
        disbursement_amount=_disbursement_amount_for_contract(principal, row.product_type),
        fixed_markup_amount=fixed_markup,
        term_days=row.term_days,
        monthly_interest_rate=_to_float(row.monthly_interest_rate),
        processing_fee_amount=_to_float(row.processing_fee_amount),
        interest_amount=_to_float(row.interest_amount),
        late_fee_amount=_to_float(row.late_fee_amount),
        total_due=_to_float(row.total_due),
        outstanding_amount=_to_float(row.outstanding_amount),
        duplum_cap_amount=_to_float(row.duplum_cap_amount),
        status=row.status.value,
        disbursed_at=_as_utc(row.disbursed_at),
        due_at=_as_utc(row.due_at),
        closed_at=_as_utc(row.closed_at),
        created_at=_as_utc(row.created_at),
    )


def _serialize_repayment(row: Repayment) -> RepaymentOut:
    return RepaymentOut(
        id=row.id,
        loan_id=row.loan_id,
        amount=_to_float(row.amount),
        channel=row.channel,
        paid_to_phone=row.paid_to_phone,
        reference=row.reference,
        created_at=_as_utc(row.created_at),
    )


def _distribution_base_url(request: Request) -> str:
    configured = (settings.distribution_public_base_url or "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _distribution_install_url(request: Request) -> str:
    install_path = settings.distribution_install_page_path or "/install"
    normalized_path = install_path if install_path.startswith("/") else f"/{install_path}"
    return f"{_distribution_base_url(request)}{normalized_path}"


def _distribution_apk_url(request: Request) -> str:
    return f"{_distribution_base_url(request)}/downloads/{settings.distribution_apk_filename}"


def _distribution_whatsapp_share_url(request: Request) -> str:
    install_url = _distribution_install_url(request)
    apk_url = _distribution_apk_url(request)
    raw_template = settings.distribution_whatsapp_share_text or (
        "Install Aflex Loan app here: {install_url} . Download APK directly: {apk_url}"
    )
    message = raw_template.format(install_url=install_url, apk_url=apk_url)
    return f"https://wa.me/?text={quote_plus(message)}"


def _distribution_links_payload(request: Request) -> dict[str, str]:
    return {
        "brand_name": settings.distribution_brand_name,
        "install_url": _distribution_install_url(request),
        "apk_url": _distribution_apk_url(request),
        "whatsapp_share_url": _distribution_whatsapp_share_url(request),
    }


def _distribution_downloads_dir() -> Path:
    configured = settings.distribution_downloads_dir or "./downloads"
    candidate = Path(configured)
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    return candidate


def _distribution_apk_path() -> Path:
    return _distribution_downloads_dir() / settings.distribution_apk_filename


def _ensure_distribution_assets() -> None:
    _distribution_downloads_dir().mkdir(parents=True, exist_ok=True)


def _database_table_exists(table_name: str) -> bool:
    return inspect(engine).has_table(table_name)


def _bootstrap_admin_user() -> None:
    email = (settings.bootstrap_admin_email or "").strip().lower()
    password = (settings.bootstrap_admin_password or "").strip()
    if not email and not password:
        return
    if not email or not password:
        logger.warning(
            "Skipping admin bootstrap because BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD must both be set"
        )
        return

    phone = _normalize_kenyan_phone_or_empty(settings.bootstrap_admin_phone)
    db = SessionLocal()
    try:
        existing = db.query(User).filter(func.lower(User.email) == email).first()
        if existing:
            if existing.role != UserRole.admin:
                logger.warning("Bootstrap admin email=%s already belongs to a non-admin user; leaving account unchanged", email)
            return

        admin = User(
            email=email,
            phone=phone or None,
            full_name=(settings.bootstrap_admin_full_name or "Aflex Loans Admin").strip() or "Aflex Loans Admin",
            role=UserRole.admin,
            password_hash=hash_password(password),
            is_active=True,
        )
        db.add(admin)
        db.flush()
        write_audit_log(
            db,
            action="admin.bootstrap",
            entity_type="user",
            entity_id=admin.id,
            user_id=admin.id,
            metadata={"email": email},
        )
        db.commit()
        logger.info("Bootstrapped admin account email=%s", email)
    finally:
        db.close()


@app.on_event("startup")
def startup() -> None:
    global _REMINDER_WORKER_STARTED
    _ensure_distribution_assets()
    config_issues = settings.validate_runtime_configuration()
    if config_issues:
        raise RuntimeError("Invalid runtime configuration: " + "; ".join(config_issues))

    try:
        users_table_ready = _database_table_exists("users")
    except Exception:
        logger.exception("Database connectivity check failed during startup")
        raise

    if not users_table_ready:
        logger.warning("Skipping DB-dependent startup tasks because migrations have not been applied yet")
        return

    _bootstrap_admin_user()

    if settings.due_reminder_enabled:
        if not _database_table_exists("loans"):
            logger.warning("Skipping due-reminder worker because migrations have not created the loans table yet")
            return
        _run_due_soon_reminders_once(trigger="startup")
        if not _REMINDER_WORKER_STARTED:
            worker = threading.Thread(target=_due_reminder_worker, name="aflex-due-reminder-worker", daemon=True)
            worker.start()
            _REMINDER_WORKER_STARTED = True


@app.get("/")
def root() -> dict[str, str]:
    return {"name": settings.app_name, "status": "running"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(settings.distribution_install_page_path, response_class=HTMLResponse)
def install_page(request: Request) -> HTMLResponse:
    links = _distribution_links_payload(request)
    apk_path = _distribution_apk_path()
    apk_exists = apk_path.exists()
    support_phone = (settings.distribution_support_phone or "").strip() or "-"
    support_email = (settings.distribution_support_email or "").strip() or "-"
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(settings.distribution_brand_name)} Android Install</title>
  <style>
    :root {{
      --bg: #08131f;
      --card: #0e2236;
      --ink: #eaf4ff;
      --muted: #97b1cc;
      --accent: #2dd4bf;
      --accent2: #f59e0b;
      --line: #224160;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Tahoma, sans-serif;
      color: var(--ink);
      background: radial-gradient(circle at top right, #173450 0, #08131f 55%);
      min-height: 100vh;
      padding: 20px;
    }}
    .shell {{
      max-width: 760px;
      margin: 0 auto;
      background: linear-gradient(145deg, #10273d 0%, #0c1f32 100%);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 20px;
      box-shadow: 0 16px 40px rgba(0,0,0,0.35);
    }}
    .title {{ margin: 0 0 8px; font-size: 1.7rem; }}
    .sub {{ margin: 0 0 16px; color: var(--muted); }}
    .row {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .btn {{
      display: inline-block;
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
      text-align: center;
      text-decoration: none;
      font-weight: 700;
      color: #0e1f30;
      background: var(--accent);
    }}
    .btn.alt {{
      background: var(--accent2);
      color: #2b1600;
    }}
    .btn.ghost {{
      background: transparent;
      color: var(--ink);
    }}
    .pill {{
      display: inline-block;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      font-size: 0.82rem;
      margin-bottom: 12px;
      color: var(--muted);
    }}
    ul {{
      margin: 0 0 12px 18px;
      color: var(--muted);
      line-height: 1.5;
    }}
    .foot {{
      margin-top: 14px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 0.92rem;
    }}
    code {{
      display: block;
      background: #091826;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
      overflow-wrap: anywhere;
      color: #c5def5;
      margin-bottom: 10px;
    }}
  </style>
</head>
<body>
  <main class="shell">
    <div class="pill">Kenya Android Installer</div>
    <h1 class="title">{html.escape(settings.distribution_brand_name)} App Download</h1>
    <p class="sub">Install the latest Android APK and share this page on WhatsApp. All loan form submissions sync to your Aflex backend server.</p>
    <div class="row">
      <a class="btn" href="{html.escape(links["apk_url"])}">Download APK</a>
      <a class="btn alt" href="{html.escape(links["whatsapp_share_url"])}">Share on WhatsApp</a>
      <a class="btn ghost" href="{html.escape(links["install_url"])}">Copy Install Link</a>
    </div>
    <ul>
      <li>APK file status: {"Ready" if apk_exists else "Not uploaded yet"}</li>
      <li>If install is blocked, enable <b>Install unknown apps</b> in Android settings.</li>
      <li>After login and loan application, data is saved in your server database on this computer.</li>
    </ul>
    <p class="sub" style="margin-bottom:6px">Direct install link:</p>
    <code>{html.escape(links["install_url"])}</code>
    <p class="sub" style="margin-bottom:6px">Direct APK link:</p>
    <code>{html.escape(links["apk_url"])}</code>
    <div class="foot">
      Support phone: {html.escape(support_phone)} | Support email: {html.escape(support_email)}
    </div>
  </main>
</body>
</html>
"""
    return HTMLResponse(content=html_doc)


@app.get("/downloads/{filename}")
def download_apk(filename: str) -> FileResponse:
    if filename != settings.distribution_apk_filename:
        raise HTTPException(status_code=404, detail="File not found")
    apk_path = _distribution_apk_path()
    if not apk_path.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"{settings.distribution_apk_filename} has not been uploaded yet. "
                "Build the APK and copy it to apps/api/downloads."
            ),
        )
    media_type = mimetypes.guess_type(apk_path.name)[0] or "application/vnd.android.package-archive"
    return FileResponse(path=str(apk_path), media_type=media_type, filename=apk_path.name)


@app.get(f"{settings.api_v1_prefix}/distribution/links")
def distribution_links(request: Request) -> dict[str, object]:
    links = _distribution_links_payload(request)
    apk_path = _distribution_apk_path()
    return {
        **links,
        "apk_filename": settings.distribution_apk_filename,
        "apk_ready": apk_path.exists(),
        "downloads_dir": str(_distribution_downloads_dir()),
    }


@app.get(f"{settings.api_v1_prefix}/market/rules", response_model=list[LoanProductRule])
def market_rules() -> list[LoanProductRule]:
    return _product_rules()


@app.post(f"{settings.api_v1_prefix}/auth/otp/request", response_model=AuthOtpRequestOut)
def request_phone_otp(payload: AuthOtpRequest, request: Request, db: Session = Depends(get_db)) -> AuthOtpRequestOut:
    now = _utc_now()
    _prune_otp_state(now)

    normalized_phone = _normalize_phone_number(payload.phone)
    if not is_valid_kenyan_phone(normalized_phone):
        raise HTTPException(status_code=422, detail="Invalid Kenyan phone number")

    effective_email = (str(payload.email).strip().lower() if payload.email else None)
    requested_channels = payload.channels or [payload.channel]
    requested_channels = list(dict.fromkeys(requested_channels))
    if payload.purpose == "register":
        if not effective_email:
            raise HTTPException(status_code=422, detail="Email is required for registration OTP delivery")
        existing_phone = db.query(User).filter(User.phone == normalized_phone).first()
        if existing_phone:
            raise HTTPException(status_code=409, detail="Phone already registered.")
        existing_email = db.query(User).filter(func.lower(User.email) == effective_email).first()
        if existing_email:
            raise HTTPException(status_code=409, detail="Email already registered.")
        if "email" not in requested_channels:
            requested_channels.append("email")

    if not requested_channels:
        raise HTTPException(status_code=422, detail="At least one OTP channel is required")
    if "email" in requested_channels and not effective_email:
        raise HTTPException(status_code=422, detail="Email is required for email OTP delivery")

    last_sent = OTP_LAST_SENT_AT.get(normalized_phone)
    if last_sent:
        seconds_since_last = int((now - last_sent).total_seconds())
        retry_after_seconds = OTP_RESEND_SECONDS - seconds_since_last
        if retry_after_seconds > 0:
            raise HTTPException(
                status_code=429,
                detail=f"OTP recently requested. Retry in {retry_after_seconds} seconds.",
            )

    otp_code = "".join(str(random.randint(0, 9)) for _ in range(OTP_LENGTH))
    challenge_id = secrets.token_urlsafe(20)
    expires_at = now + timedelta(seconds=OTP_EXPIRY_SECONDS)
    delivery_results: dict[str, str] = {}
    delivery_providers: dict[str, str] = {}
    successful_channels: list[str] = []
    failed_channels: list[str] = []
    for channel in requested_channels:
        sent, delivery_message, delivery_provider = _send_otp(
            phone=normalized_phone,
            otp_code=otp_code,
            channel=channel,
            email=effective_email,
        )
        delivery_results[channel] = delivery_message
        delivery_providers[channel] = delivery_provider
        if sent:
            successful_channels.append(channel)
        else:
            failed_channels.append(channel)

    if not successful_channels:
        details = "; ".join(f"{channel}: {delivery_results[channel]}" for channel in failed_channels)
        raise HTTPException(status_code=503, detail=f"OTP delivery failed ({details})")

    message = "OTP sent via " + ", ".join(successful_channels)
    if failed_channels:
        message += ". Unavailable: " + ", ".join(failed_channels)

    OTP_CHALLENGES[challenge_id] = {
        "phone": normalized_phone,
        "purpose": payload.purpose,
        "channel": successful_channels[0],
        "channels": successful_channels,
        "providers": delivery_providers,
        "email": effective_email,
        "otp_code": otp_code,
        "attempts": 0,
        "expires_at": expires_at,
        "created_at": now,
    }
    OTP_LAST_SENT_AT[normalized_phone] = now

    response = AuthOtpRequestOut(
        challenge_id=challenge_id,
        phone=normalized_phone,
        purpose=payload.purpose,
        channel=successful_channels[0],
        channels=successful_channels,
        email=effective_email,
        expires_in_seconds=OTP_EXPIRY_SECONDS,
        retry_after_seconds=OTP_RESEND_SECONDS,
        message=message,
        delivery_results=delivery_results,
    )
    if _is_local_request(request) and settings.otp_allow_debug_code:
        response.debug_code = otp_code
    return response


@app.post(f"{settings.api_v1_prefix}/auth/otp/verify", response_model=AuthOtpVerifyOut)
def verify_phone_otp(payload: AuthOtpVerifyRequest) -> AuthOtpVerifyOut:
    now = _utc_now()
    challenge = OTP_CHALLENGES.get(payload.challenge_id)
    if not challenge:
        raise HTTPException(status_code=404, detail="OTP challenge not found")

    expires_at = challenge.get("expires_at")
    if not expires_at or expires_at < now:
        OTP_CHALLENGES.pop(payload.challenge_id, None)
        raise HTTPException(status_code=422, detail="OTP challenge expired")

    attempts = int(challenge.get("attempts", 0))
    if attempts >= OTP_MAX_ATTEMPTS:
        OTP_CHALLENGES.pop(payload.challenge_id, None)
        raise HTTPException(status_code=429, detail="OTP attempts exceeded")

    if payload.otp_code != challenge.get("otp_code"):
        challenge["attempts"] = attempts + 1
        raise HTTPException(status_code=401, detail="Invalid OTP code")

    verified = AuthOtpVerifyOut(
        challenge_id=payload.challenge_id,
        verified=True,
        phone=str(challenge["phone"]),
        purpose=str(challenge["purpose"]),
        verified_at=now,
    )
    OTP_CHALLENGES.pop(payload.challenge_id, None)
    return verified


@app.post(f"{settings.api_v1_prefix}/auth/register", response_model=UserOut, status_code=201)
def register(payload: UserRegisterRequest, db: Session = Depends(get_db)) -> UserOut:
    normalized_phone = _normalize_phone_number(payload.phone)
    normalized_email = payload.email.strip().lower()
    if not is_valid_kenyan_phone(normalized_phone):
        raise HTTPException(status_code=422, detail="Invalid Kenyan phone number")

    existing = db.query(User).filter(User.phone == normalized_phone).first()
    if existing:
        raise HTTPException(status_code=409, detail="Phone already registered.")

    existing_email = db.query(User).filter(func.lower(User.email) == normalized_email).first()
    if existing_email:
        raise HTTPException(status_code=409, detail="Email already registered.")

    user = User(
        email=normalized_email,
        phone=normalized_phone,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role=UserRole.borrower,
    )
    db.add(user)
    write_audit_log(
        db,
        action="user.register",
        entity_type="user",
        entity_id=user.id,
        user_id=user.id,
        metadata={"phone": normalized_phone, "email": normalized_email},
    )
    db.commit()
    db.refresh(user)
    return user


@app.post(f"{settings.api_v1_prefix}/auth/login", response_model=Token)
def login(payload: UserLoginRequest, db: Session = Depends(get_db)) -> Token:
    login_identifier = payload.phone.strip().lower()
    if "@" in login_identifier:
        user = db.query(User).filter(func.lower(User.email) == login_identifier).first()
    else:
        normalized_phone = _normalize_phone_number(payload.phone)
        if not is_valid_kenyan_phone(normalized_phone):
            raise HTTPException(status_code=422, detail="Invalid Kenyan phone number")

        user = db.query(User).filter(User.phone == normalized_phone).first()
        if not user:
            legacy_email = _phone_to_internal_email(normalized_phone)
            user = db.query(User).filter(User.email == legacy_email).first()
            if user and not user.phone:
                user.phone = normalized_phone
                db.commit()
                db.refresh(user)

    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not active")
    access_token = create_access_token(user.id)
    refresh_token = create_refresh_token(user.id)
    return Token(access_token=access_token, refresh_token=refresh_token)


@app.post(f"{settings.api_v1_prefix}/auth/refresh", response_model=Token)
def refresh_token(payload: RefreshTokenRequest, db: Session = Depends(get_db)) -> Token:
    decoded = decode_refresh_token(payload.refresh_token)
    if not decoded or "sub" not in decoded:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    user = db.query(User).filter(User.id == decoded["sub"]).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not active")
    return Token(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@app.get(f"{settings.api_v1_prefix}/auth/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> UserOut:
    return current_user


@app.post(f"{settings.api_v1_prefix}/profile", response_model=BorrowerProfileOut)
def upsert_profile(
    payload: BorrowerProfileUpsert,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BorrowerProfileOut:
    normalized_phone = _normalize_phone_number(payload.phone)
    if not is_valid_kenyan_phone(normalized_phone):
        raise HTTPException(status_code=422, detail="Invalid Kenyan profile phone number")
    if current_user.phone and current_user.phone != normalized_phone:
        raise HTTPException(status_code=422, detail="Profile phone must match account phone")

    payload_data = payload.model_dump()
    payload_data["phone"] = normalized_phone
    notification_email = (payload_data.get("notification_email") or "").strip()
    if not notification_email:
        raise HTTPException(status_code=422, detail="Notification email is required for onboarding")
    payload_data["notification_email"] = notification_email
    if not current_user.phone:
        current_user.phone = normalized_phone

    row = db.query(BorrowerProfile).filter(BorrowerProfile.user_id == current_user.id).first()
    if not row:
        row = BorrowerProfile(user_id=current_user.id, **payload_data)
        db.add(row)
    else:
        for key, value in payload_data.items():
            setattr(row, key, value)
    _flag_if_fraud_signals(db, row)
    write_audit_log(
        db,
        action="profile.upsert",
        entity_type="borrower_profile",
        entity_id=row.id,
        user_id=current_user.id,
    )
    db.commit()
    db.refresh(row)
    return row


@app.get(f"{settings.api_v1_prefix}/profile", response_model=BorrowerProfileOut)
def get_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BorrowerProfileOut:
    row = db.query(BorrowerProfile).filter(BorrowerProfile.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    return row


@app.post(f"{settings.api_v1_prefix}/consents", response_model=ConsentOut, status_code=201)
def add_consent(
    payload: ConsentRequest,
    request: Request,
    user_agent: str | None = Header(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConsentOut:
    consent = Consent(
        user_id=current_user.id,
        consent_type=payload.consent_type,
        accepted=payload.accepted,
        ip_address=request.client.host if request.client else None,
        user_agent=user_agent,
    )
    db.add(consent)
    write_audit_log(
        db,
        action="consent.recorded",
        entity_type="consent",
        entity_id=consent.id,
        user_id=current_user.id,
        metadata={"consent_type": payload.consent_type, "accepted": payload.accepted},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(consent)
    return consent


@app.get(f"{settings.api_v1_prefix}/consents", response_model=list[ConsentOut])
def list_consents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ConsentOut]:
    rows = db.query(Consent).filter(Consent.user_id == current_user.id).order_by(Consent.accepted_at.desc()).all()
    return rows


@app.post(f"{settings.api_v1_prefix}/loans/applications", response_model=LoanApplicationOut, status_code=201)
def create_application(
    payload: LoanApplicationCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LoanApplicationOut:
    if payload.product_type != "personal":
        raise HTTPException(status_code=422, detail="Aflex currently issues personal loans only")
    requested_disbursement_amount = round(float(payload.requested_amount), 2)
    _validate_product_input(
        product_type=payload.product_type,
        term_days=payload.term_days,
        disbursement_amount=requested_disbursement_amount,
    )
    _ensure_required_consents(db, current_user.id)

    if _active_loans_count(db, current_user.id) >= settings.max_active_loans_per_user:
        raise HTTPException(
            status_code=422,
            detail=f"Maximum active loans reached ({settings.max_active_loans_per_user})",
        )

    if _daily_applications_count(db, current_user.id) >= settings.max_daily_applications_per_user:
        raise HTTPException(
            status_code=429,
            detail=f"Daily application limit reached ({settings.max_daily_applications_per_user})",
        )

    profile = db.query(BorrowerProfile).filter(BorrowerProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=422, detail="Complete KYC profile first")
    common_required_fields = {
        "M-Pesa phone": profile.mpesa_phone,
        "Notification email": profile.notification_email,
        "Residential address": profile.residential_address,
        "Location landmark": profile.location_landmark,
        "ID front hash": profile.id_front_hash,
        "ID back hash": profile.id_back_hash,
        "Selfie hash": profile.selfie_image_hash,
    }
    if payload.product_type == "personal":
        track_required_fields = {
            "Next of kin name": profile.next_of_kin_name,
            "Next of kin phone": profile.next_of_kin_phone,
        }
    else:
        business_age_value = profile.business_age_months if (profile.business_age_months or 0) > 0 else None
        track_required_fields = {
            "Business name": profile.business_name,
            "Business age (months)": business_age_value,
            "Business photo hash": profile.business_photo_hash,
        }

    required_fields = {**common_required_fields, **track_required_fields}
    missing_fields = [label for label, value in required_fields.items() if not value]
    if missing_fields:
        raise HTTPException(
            status_code=422,
            detail=f"Complete {payload.product_type} onboarding fields first: {', '.join(missing_fields)}",
        )
    if not profile.is_id_verified:
        raise HTTPException(status_code=422, detail="ID verification required before application")
    if not profile.is_selfie_verified:
        raise HTTPException(status_code=422, detail="Selfie verification required before application")
    if not profile.is_location_verified:
        raise HTTPException(status_code=422, detail="Location verification required before application")
    if profile.gps_lat is None or profile.gps_lng is None:
        raise HTTPException(status_code=422, detail="Capture your current GPS location before application")
    if profile.location_accuracy_m is None:
        raise HTTPException(status_code=422, detail="Location accuracy missing. Recapture location from this device")
    if profile.location_accuracy_m > 150:
        raise HTTPException(
            status_code=422,
            detail="Location accuracy is too low. Move outdoors and recapture location (<=150m)",
        )
    captured_at = _as_utc(profile.location_captured_at)
    if captured_at is None:
        raise HTTPException(status_code=422, detail="Location capture timestamp missing. Recapture location")
    if captured_at < _utc_now() - timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="Location capture is older than 24 hours. Recapture location before application",
        )

    contract_amount = _contract_amount_for_disbursement(requested_disbursement_amount, payload.product_type)
    application = LoanApplication(
        user_id=current_user.id,
        product_type=ProductType(payload.product_type),
        requested_amount=contract_amount,
        term_days=payload.term_days,
        purpose=payload.purpose,
        status=ApplicationStatus.pending,
    )
    db.add(application)
    db.flush()

    _flag_if_fraud_signals(db, profile, application_id=application.id)
    assessment = _risk_assessment_for(db=db, profile=profile, application=application)

    application.risk_score = assessment.risk_score
    application.risk_band = assessment.risk_band
    recommended_limit = _to_float(assessment.max_offer_amount)
    payment_proof_present = bool(profile.payment_proof_hash and profile.payment_proof_type)
    repaid_loans_count = _count_repaid_loans(db, current_user.id)
    if payload.product_type == "personal":
        if payment_proof_present:
            recommended_limit = min(
                float(settings.personal_max_amount),
                round(recommended_limit * 1.2, 2),
            )
        else:
            recommended_limit = min(recommended_limit, 5000.0)
    if repaid_loans_count > 0:
        growth_multiplier = min(
            float(settings.loyalty_limit_max_multiplier),
            1.0 + (float(settings.loyalty_limit_increment_rate) * repaid_loans_count),
        )
        boosted_limit = max(
            round(recommended_limit * growth_multiplier, 2),
            round(recommended_limit + float(settings.loyalty_limit_min_bonus_amount), 2),
        )
        product_cap = float(settings.personal_max_amount) if payload.product_type == "personal" else float(settings.business_max_amount)
        recommended_limit = min(product_cap, boosted_limit)
    application.recommended_limit = recommended_limit
    if assessment.approved:
        application.status = ApplicationStatus.reviewed
    else:
        application.status = ApplicationStatus.rejected
        application.rejection_reason = "; ".join(assessment.reasons[:3])

    write_audit_log(
        db,
        action="application.created",
        entity_type="loan_application",
        entity_id=application.id,
        user_id=current_user.id,
        metadata={
            "product_type": payload.product_type,
            "requested_disbursement_amount": requested_disbursement_amount,
            "upfront_fee_amount": _upfront_fee_amount_for_contract(contract_amount, payload.product_type),
            "requested_amount": contract_amount,
            "term_days": payload.term_days,
            "risk_score": assessment.risk_score,
            "risk_band": assessment.risk_band,
            "approved": assessment.approved,
            "payment_proof_present": payment_proof_present,
            "recommended_limit": recommended_limit,
            "repaid_loans_count": repaid_loans_count,
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(application)

    try:
        alert_result = _notify_admin_new_application(user=current_user, profile=profile, application=application)
        write_audit_log(
            db,
            action="application.admin_alert",
            entity_type="loan_application",
            entity_id=application.id,
            user_id=current_user.id,
            metadata=alert_result,
            ip_address=request.client.host if request.client else None,
        )
        db.commit()
    except Exception as error:
        write_audit_log(
            db,
            action="application.admin_alert.failed",
            entity_type="loan_application",
            entity_id=application.id,
            user_id=current_user.id,
            metadata={"error": str(error)[:220]},
            ip_address=request.client.host if request.client else None,
        )
        db.commit()

    return _serialize_application(application)


@app.get(f"{settings.api_v1_prefix}/loans/applications/me", response_model=list[LoanApplicationOut])
def my_applications(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LoanApplicationOut]:
    rows = (
        db.query(LoanApplication)
        .filter(LoanApplication.user_id == current_user.id)
        .order_by(LoanApplication.created_at.desc())
        .all()
    )
    return [_serialize_application(row) for row in rows]


@app.get(f"{settings.api_v1_prefix}/admin/loans/applications", response_model=list[AdminLoanApplicationOut])
def admin_applications_queue(
    status_filter: ApplicationStatus | None = ApplicationStatus.reviewed,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[AdminLoanApplicationOut]:
    rows = (
        db.query(LoanApplication, User, BorrowerProfile)
        .join(User, User.id == LoanApplication.user_id)
        .outerjoin(BorrowerProfile, BorrowerProfile.user_id == LoanApplication.user_id)
        .order_by(LoanApplication.created_at.desc())
    )
    if status_filter is not None:
        rows = rows.filter(LoanApplication.status == status_filter)
    results = rows.limit(200).all()
    return [
        _serialize_admin_application(row=application, borrower=borrower, profile=profile)
        for application, borrower, profile in results
    ]


@app.get(f"{settings.api_v1_prefix}/loans/applications/{{application_id}}/risk", response_model=RiskAssessmentOut)
def application_risk(
    application_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RiskAssessmentOut:
    application = (
        db.query(LoanApplication)
        .filter(LoanApplication.id == application_id, LoanApplication.user_id == current_user.id)
        .first()
    )
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    profile = db.query(BorrowerProfile).filter(BorrowerProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=422, detail="Profile not found")
    return _risk_assessment_for(db=db, profile=profile, application=application)


@app.post(f"{settings.api_v1_prefix}/admin/loans/applications/{{application_id}}/approve", response_model=LoanOut)
def approve_application_as_admin(
    application_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> LoanOut:
    application = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    if application.status == ApplicationStatus.rejected:
        raise HTTPException(status_code=422, detail=f"Application rejected: {application.rejection_reason}")
    if not application.risk_band or application.recommended_limit is None:
        raise HTTPException(status_code=422, detail="Application risk assessment not completed")

    borrower = db.query(User).filter(User.id == application.user_id).first()
    if not borrower:
        raise HTTPException(status_code=404, detail="Borrower not found")
    profile = db.query(BorrowerProfile).filter(BorrowerProfile.user_id == borrower.id).first()

    existing_loan = db.query(Loan).filter(Loan.application_id == application.id).first()
    if existing_loan:
        return _serialize_loan(existing_loan)

    offer = db.query(LoanOffer).filter(LoanOffer.application_id == application.id).first()
    if not offer:
        principal = min(_to_float(application.requested_amount), _to_float(application.recommended_limit))
        if principal <= 0:
            raise HTTPException(status_code=422, detail="No eligible loan amount based on risk")

        pricing = compute_pricing(
            settings=settings,
            product_type=application.product_type,
            principal=principal,
            term_days=application.term_days,
            risk_band=application.risk_band,
        )
        offer = LoanOffer(
            application_id=application.id,
            principal_amount=pricing.principal,
            term_days=pricing.term_days,
            monthly_interest_rate=pricing.monthly_interest_rate,
            processing_fee_rate=pricing.processing_fee_rate,
            processing_fee_amount=pricing.processing_fee_amount,
            interest_amount=pricing.interest_amount,
            late_fee_amount=pricing.late_fee_amount,
            total_due=pricing.total_due,
            duplum_cap_amount=pricing.duplum_cap_amount,
            status=OfferStatus.accepted,
            expires_at=_utc_now() + timedelta(hours=24),
        )
        db.add(offer)
        db.flush()
    else:
        offer.status = OfferStatus.accepted

    loan = Loan(
        user_id=borrower.id,
        application_id=application.id,
        offer_id=offer.id,
        product_type=application.product_type,
        principal_amount=offer.principal_amount,
        term_days=offer.term_days,
        monthly_interest_rate=offer.monthly_interest_rate,
        processing_fee_amount=offer.processing_fee_amount,
        interest_amount=offer.interest_amount,
        late_fee_amount=offer.late_fee_amount,
        total_due=offer.total_due,
        outstanding_amount=offer.total_due,
        duplum_cap_amount=offer.duplum_cap_amount,
        status=LoanStatus.pending_disbursement,
        due_at=_utc_now() + timedelta(days=offer.term_days),
    )
    application.status = ApplicationStatus.approved
    db.add(loan)
    db.flush()

    write_audit_log(
        db,
        action="application.approved",
        entity_type="loan_application",
        entity_id=application.id,
        user_id=admin.id,
        metadata={"loan_id": loan.id, "offer_id": offer.id, "admin_id": admin.id},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(loan)

    try:
        notify_result = _notify_borrower_loan_approved(
            user=borrower,
            profile=profile,
            loan=loan,
            application=application,
        )
        write_audit_log(
            db,
            action="loan.approval_notification",
            entity_type="loan",
            entity_id=loan.id,
            user_id=admin.id,
            metadata=notify_result,
            ip_address=request.client.host if request.client else None,
        )
    except Exception as error:
        write_audit_log(
            db,
            action="loan.approval_notification.failed",
            entity_type="loan",
            entity_id=loan.id,
            user_id=admin.id,
            metadata={"error": str(error)[:220]},
            ip_address=request.client.host if request.client else None,
        )
    db.commit()
    return _serialize_loan(loan)


@app.post(f"{settings.api_v1_prefix}/loans/applications/{{application_id}}/offer", response_model=LoanOfferOut)
def generate_offer(
    application_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LoanOfferOut:
    application = (
        db.query(LoanApplication)
        .filter(LoanApplication.id == application_id, LoanApplication.user_id == current_user.id)
        .first()
    )
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    if application.status == ApplicationStatus.rejected:
        raise HTTPException(status_code=422, detail=f"Application rejected: {application.rejection_reason}")

    if application.offer and application.offer.status in {OfferStatus.offered, OfferStatus.accepted}:
        return _serialize_offer(application.offer)

    if not application.risk_band or application.recommended_limit is None:
        raise HTTPException(status_code=422, detail="Application risk assessment not completed")

    principal = min(_to_float(application.requested_amount), _to_float(application.recommended_limit))
    if principal <= 0:
        raise HTTPException(status_code=422, detail="No eligible loan amount based on risk")

    pricing = compute_pricing(
        settings=settings,
        product_type=application.product_type,
        principal=principal,
        term_days=application.term_days,
        risk_band=application.risk_band,
    )

    offer = LoanOffer(
        application_id=application.id,
        principal_amount=pricing.principal,
        term_days=pricing.term_days,
        monthly_interest_rate=pricing.monthly_interest_rate,
        processing_fee_rate=pricing.processing_fee_rate,
        processing_fee_amount=pricing.processing_fee_amount,
        interest_amount=pricing.interest_amount,
        late_fee_amount=pricing.late_fee_amount,
        total_due=pricing.total_due,
        duplum_cap_amount=pricing.duplum_cap_amount,
        status=OfferStatus.offered,
        expires_at=_utc_now() + timedelta(hours=24),
    )
    db.add(offer)
    application.status = ApplicationStatus.approved

    write_audit_log(
        db,
        action="offer.generated",
        entity_type="loan_offer",
        entity_id=offer.id,
        user_id=current_user.id,
        metadata={
            "principal": pricing.principal,
            "total_due": pricing.total_due,
            "monthly_interest_rate": pricing.monthly_interest_rate,
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(offer)
    return _serialize_offer(offer)


@app.get(f"{settings.api_v1_prefix}/loans/offers/me", response_model=list[LoanOfferOut])
def my_offers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LoanOfferOut]:
    rows = (
        db.query(LoanOffer)
        .join(LoanApplication, LoanApplication.id == LoanOffer.application_id)
        .filter(LoanApplication.user_id == current_user.id)
        .order_by(LoanOffer.created_at.desc())
        .all()
    )
    return [_serialize_offer(row) for row in rows]


@app.post(f"{settings.api_v1_prefix}/loans/offers/{{offer_id}}/accept", response_model=LoanOut)
def accept_offer(
    offer_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LoanOut:
    offer = (
        db.query(LoanOffer)
        .join(LoanApplication, LoanApplication.id == LoanOffer.application_id)
        .filter(LoanOffer.id == offer_id, LoanApplication.user_id == current_user.id)
        .first()
    )
    if not offer:
        raise HTTPException(status_code=404, detail="Offer not found")
    if offer.status != OfferStatus.offered:
        raise HTTPException(status_code=422, detail="Offer is not available for acceptance")
    offer_expires_at = _as_utc(offer.expires_at)
    if offer_expires_at is None:
        raise HTTPException(status_code=422, detail="Offer expiry missing")
    if offer_expires_at < _utc_now():
        offer.status = OfferStatus.expired
        db.commit()
        raise HTTPException(status_code=422, detail="Offer expired")

    application = db.query(LoanApplication).filter(LoanApplication.id == offer.application_id).first()
    existing_loan = db.query(Loan).filter(Loan.offer_id == offer.id).first()
    if existing_loan:
        return _serialize_loan(existing_loan)

    disbursed_at = _utc_now()
    loan = Loan(
        user_id=current_user.id,
        application_id=application.id,
        offer_id=offer.id,
        product_type=application.product_type,
        principal_amount=offer.principal_amount,
        term_days=offer.term_days,
        monthly_interest_rate=offer.monthly_interest_rate,
        processing_fee_amount=offer.processing_fee_amount,
        interest_amount=offer.interest_amount,
        late_fee_amount=offer.late_fee_amount,
        total_due=offer.total_due,
        outstanding_amount=offer.total_due,
        duplum_cap_amount=offer.duplum_cap_amount,
        status=LoanStatus.active,
        disbursed_at=disbursed_at,
        due_at=disbursed_at + timedelta(days=offer.term_days),
    )
    offer.status = OfferStatus.accepted
    db.add(loan)

    write_audit_log(
        db,
        action="offer.accepted",
        entity_type="loan",
        entity_id=loan.id,
        user_id=current_user.id,
        metadata={"offer_id": offer.id},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(loan)
    return _serialize_loan(loan)


@app.get(f"{settings.api_v1_prefix}/loans/me", response_model=list[LoanOut])
def my_loans(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LoanOut]:
    rows = db.query(Loan).filter(Loan.user_id == current_user.id).order_by(Loan.created_at.desc()).all()
    updated = False
    for row in rows:
        if _apply_late_interest_if_due(row):
            updated = True
    if updated:
        db.commit()
    return [_serialize_loan(row) for row in rows]


@app.post(f"{settings.api_v1_prefix}/uploadPhoto", response_model=UploadPhotoOut)
async def upload_photo(
    doc_type: str = Form(...),
    file: UploadFile = File(...),
    loan_id: str | None = Form(default=None),
    latitude: float | None = Form(default=None),
    longitude: float | None = Form(default=None),
    accuracy_m: float | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UploadPhotoOut:
    normalized_doc_type = doc_type.strip().lower()
    if not normalized_doc_type:
        raise HTTPException(status_code=422, detail="doc_type is required")
    if latitude is None and longitude is not None:
        raise HTTPException(status_code=422, detail="latitude is required when longitude is provided")
    if latitude is not None and longitude is None:
        raise HTTPException(status_code=422, detail="longitude is required when latitude is provided")
    if latitude is not None and (latitude < -90 or latitude > 90):
        raise HTTPException(status_code=422, detail="latitude must be between -90 and 90")
    if longitude is not None and (longitude < -180 or longitude > 180):
        raise HTTPException(status_code=422, detail="longitude must be between -180 and 180")
    if accuracy_m is not None and accuracy_m < 0:
        raise HTTPException(status_code=422, detail="accuracy_m must be positive")

    if not file.filename:
        raise HTTPException(status_code=422, detail="File name is required")
    content_type = (file.content_type or "").lower()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=422, detail="Only image uploads are supported")

    max_bytes = max(1, settings.upload_max_mb) * 1024 * 1024
    file_bytes = await file.read(max_bytes + 1)
    if not file_bytes:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    if len(file_bytes) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.upload_max_mb}MB limit")

    loan: Loan | None = None
    if loan_id:
        loan = db.query(Loan).filter(Loan.id == loan_id, Loan.user_id == current_user.id).first()
        if not loan:
            raise HTTPException(status_code=404, detail="Loan not found")

    try:
        uploaded = upload_photo_bytes(file_bytes=file_bytes, filename=file.filename, folder="aflex-loans/photos")
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=f"Upload failed: {error}") from error

    record = DocumentPhoto(
        user_id=current_user.id,
        loan_id=loan.id if loan else None,
        doc_type=normalized_doc_type,
        photo_url=uploaded["url"],
        provider=uploaded["provider"],
    )
    db.add(record)

    if latitude is not None and longitude is not None:
        db.add(
            LocationRecord(
                user_id=current_user.id,
                latitude=latitude,
                longitude=longitude,
                accuracy_m=accuracy_m,
                captured_at=_utc_now(),
            )
        )

    write_audit_log(
        db,
        action="photo.uploaded",
        entity_type="documents_photos",
        entity_id=record.id,
        user_id=current_user.id,
        metadata={
            "doc_type": normalized_doc_type,
            "loan_id": loan_id,
            "provider": uploaded["provider"],
            "photo_url": uploaded["url"],
        },
    )
    db.commit()
    db.refresh(record)
    return record


@app.post(f"{settings.api_v1_prefix}/createLoan", response_model=LoanApplicationOut, status_code=201)
def create_loan_v2(
    payload: CreateLoanRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LoanApplicationOut:
    mapped = LoanApplicationCreate(
        product_type=payload.product_type,
        requested_amount=payload.amount,
        term_days=payload.term_days,
        purpose=payload.purpose,
    )
    return create_loan_application(mapped, request, db, current_user)


@app.post(f"{settings.api_v1_prefix}/approveLoan", response_model=LoanOut)
def approve_loan_v2(
    payload: ApproveLoanRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> LoanOut:
    return approve_application_as_admin(payload.application_id, request, db, admin)


@app.post(f"{settings.api_v1_prefix}/repayLoan", response_model=RepaymentOut)
def repay_loan_v2(
    payload: RepayLoanRequestV2,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RepaymentOut:
    request_payload = RepaymentRequest(
        amount=payload.amount,
        channel=payload.channel,
        paid_to_phone=payload.paid_to_phone,
        reference=payload.reference,
    )
    return repay_loan(payload.loan_id, request_payload, request, db, current_user)


@app.get(f"{settings.api_v1_prefix}/userProfile", response_model=BorrowerProfileOut)
def user_profile_v2(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BorrowerProfileOut:
    return get_profile(db, current_user)


@app.get(f"{settings.api_v1_prefix}/listLoans", response_model=list[LoanOut])
def list_loans_v2(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LoanOut]:
    return my_loans(db, current_user)


@app.get(f"{settings.api_v1_prefix}/repayments/me", response_model=list[RepaymentOut])
def my_repayments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[RepaymentOut]:
    rows = (
        db.query(Repayment)
        .join(Loan, Loan.id == Repayment.loan_id)
        .filter(Loan.user_id == current_user.id)
        .order_by(Repayment.created_at.desc())
        .limit(300)
        .all()
    )
    return [_serialize_repayment(row) for row in rows]


@app.post(f"{settings.api_v1_prefix}/loans/{{loan_id}}/repay", response_model=RepaymentOut)
def repay_loan(
    loan_id: str,
    payload: RepaymentRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RepaymentOut:
    loan = db.query(Loan).filter(Loan.id == loan_id, Loan.user_id == current_user.id).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    if loan.status not in {LoanStatus.active, LoanStatus.pending_disbursement}:
        raise HTTPException(status_code=422, detail="Loan is not repayable in current status")
    if payload.channel != "mpesa":
        raise HTTPException(status_code=422, detail="Repayments must be made via M-Pesa for this product")

    _apply_late_interest_if_due(loan)
    db.flush()

    expected_collection_phone = _expected_mpesa_collection_phone()
    paid_to_phone = _normalize_kenyan_phone_or_empty(payload.paid_to_phone) if payload.paid_to_phone else expected_collection_phone
    if paid_to_phone != expected_collection_phone:
        raise HTTPException(
            status_code=422,
            detail=f"Repayment must be sent to M-Pesa collection number {settings.mpesa_collection_phone}",
        )

    amount = round(payload.amount, 2)
    outstanding = _to_float(loan.outstanding_amount)
    if amount > outstanding:
        amount = outstanding

    repayment = Repayment(
        loan_id=loan.id,
        amount=amount,
        channel=payload.channel,
        paid_to_phone=paid_to_phone,
        reference=payload.reference,
    )
    db.add(repayment)
    db.flush()

    new_outstanding = round(max(0.0, outstanding - amount), 2)
    loan.outstanding_amount = new_outstanding
    if new_outstanding <= 0:
        loan.status = LoanStatus.repaid
        loan.closed_at = _utc_now()

    write_audit_log(
        db,
        action="loan.repayment",
        entity_type="repayment",
        entity_id=repayment.id,
        user_id=current_user.id,
        metadata={
            "loan_id": loan.id,
            "amount": amount,
            "channel": payload.channel,
            "paid_to_phone": paid_to_phone,
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(repayment)

    if new_outstanding <= 0:
        profile = db.query(BorrowerProfile).filter(BorrowerProfile.user_id == current_user.id).first()
        try:
            notify_result = _notify_borrower_limit_upgrade(user=current_user, profile=profile, loan=loan)
            write_audit_log(
                db,
                action="loan.repaid_notification",
                entity_type="loan",
                entity_id=loan.id,
                user_id=current_user.id,
                metadata=notify_result,
                ip_address=request.client.host if request.client else None,
            )
            db.commit()
        except Exception as error:
            write_audit_log(
                db,
                action="loan.repaid_notification.failed",
                entity_type="loan",
                entity_id=loan.id,
                user_id=current_user.id,
                metadata={"error": str(error)[:220]},
                ip_address=request.client.host if request.client else None,
            )
            db.commit()

    return _serialize_repayment(repayment)


@app.post(f"{settings.api_v1_prefix}/admin/loans/{{loan_id}}/disburse", response_model=LoanOut)
def disburse_loan(
    loan_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> LoanOut:
    loan = db.query(Loan).filter(Loan.id == loan_id).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    if loan.status != LoanStatus.pending_disbursement:
        raise HTTPException(status_code=422, detail="Loan not in disbursable state")

    disbursed_at = _utc_now()
    loan.status = LoanStatus.active
    loan.disbursed_at = disbursed_at
    loan.due_at = disbursed_at + timedelta(days=loan.term_days)
    loan.late_interest_days_applied = 0
    write_audit_log(
        db,
        action="loan.disbursed",
        entity_type="loan",
        entity_id=loan.id,
        user_id=admin.id,
        metadata={"admin_id": admin.id},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(loan)

    borrower = db.query(User).filter(User.id == loan.user_id).first()
    application = db.query(LoanApplication).filter(LoanApplication.id == loan.application_id).first()
    profile = db.query(BorrowerProfile).filter(BorrowerProfile.user_id == loan.user_id).first()
    if borrower and application:
        try:
            notify_result = _notify_borrower_loan_approved(
                user=borrower,
                profile=profile,
                loan=loan,
                application=application,
            )
            write_audit_log(
                db,
                action="loan.disbursed_notification",
                entity_type="loan",
                entity_id=loan.id,
                user_id=admin.id,
                metadata=notify_result,
                ip_address=request.client.host if request.client else None,
            )
            db.commit()
        except Exception as error:
            write_audit_log(
                db,
                action="loan.disbursed_notification.failed",
                entity_type="loan",
                entity_id=loan.id,
                user_id=admin.id,
                metadata={"error": str(error)[:220]},
                ip_address=request.client.host if request.client else None,
            )
            db.commit()
    return _serialize_loan(loan)


@app.get(f"{settings.api_v1_prefix}/admin/fraud-flags", response_model=list[FraudFlagOut])
def admin_fraud_flags(
    status_filter: FraudStatus | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[FraudFlagOut]:
    query = db.query(FraudFlag).order_by(FraudFlag.created_at.desc())
    if status_filter:
        query = query.filter(FraudFlag.status == status_filter)
    rows = query.limit(200).all()
    return rows


@app.post(f"{settings.api_v1_prefix}/admin/fraud-flags/{{flag_id}}/resolve", response_model=FraudFlagOut)
def resolve_fraud_flag(
    flag_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> FraudFlagOut:
    flag = db.query(FraudFlag).filter(FraudFlag.id == flag_id).first()
    if not flag:
        raise HTTPException(status_code=404, detail="Fraud flag not found")
    flag.status = FraudStatus.resolved
    flag.resolved_at = _utc_now()
    write_audit_log(
        db,
        action="fraud_flag.resolved",
        entity_type="fraud_flag",
        entity_id=flag.id,
        user_id=admin.id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(flag)
    return flag


@app.post(f"{settings.api_v1_prefix}/admin/jobs/reminders/due-soon", response_model=MessageResponse)
def run_due_soon_reminders_job(admin: User = Depends(require_admin)) -> MessageResponse:
    stats = _run_due_soon_reminders_once(trigger=f"admin:{admin.id}")
    return MessageResponse(
        message=(
            f"Due reminder run complete. "
            f"Scanned: {stats['scanned']}, Sent: {stats['sent']}, Failed: {stats['failed']}."
        )
    )


@app.get(f"{settings.api_v1_prefix}/admin/compliance/summary", response_model=ComplianceSummary)
def compliance_summary(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> ComplianceSummary:
    total_users = db.query(func.count(User.id)).scalar() or 0
    total_applications = db.query(func.count(LoanApplication.id)).scalar() or 0
    approved_applications = (
        db.query(func.count(LoanApplication.id))
        .filter(LoanApplication.status == ApplicationStatus.approved)
        .scalar()
        or 0
    )
    active_loans = (
        db.query(func.count(Loan.id)).filter(Loan.status == LoanStatus.active).scalar() or 0
    )
    defaulted_loans = (
        db.query(func.count(Loan.id)).filter(Loan.status == LoanStatus.defaulted).scalar() or 0
    )
    open_flags = (
        db.query(func.count(FraudFlag.id)).filter(FraudFlag.status == FraudStatus.open).scalar() or 0
    )
    return ComplianceSummary(
        total_users=int(total_users),
        total_applications=int(total_applications),
        approved_applications=int(approved_applications),
        active_loans=int(active_loans),
        defaulted_loans=int(defaulted_loans),
        open_fraud_flags=int(open_flags),
        disclaimer=settings.compliance_disclaimer,
    )


@app.get(f"{settings.api_v1_prefix}/compliance/privacy-permissions")
def privacy_permissions() -> dict:
    return {
        "allowed_permissions": ["camera", "location", "id_upload"],
        "forbidden_permissions": ["contacts", "sms", "gallery_scan"],
        "policy": "Consent-based minimal data collection and encrypted processing.",
    }
