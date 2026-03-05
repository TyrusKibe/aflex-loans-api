import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class UserRole(str, enum.Enum):
    borrower = "borrower"
    admin = "admin"


class ProductType(str, enum.Enum):
    personal = "personal"
    business = "business"


class ApplicationStatus(str, enum.Enum):
    pending = "pending"
    reviewed = "reviewed"
    approved = "approved"
    rejected = "rejected"


class OfferStatus(str, enum.Enum):
    offered = "offered"
    accepted = "accepted"
    expired = "expired"
    revoked = "revoked"


class LoanStatus(str, enum.Enum):
    pending_disbursement = "pending_disbursement"
    active = "active"
    repaid = "repaid"
    defaulted = "defaulted"
    cancelled = "cancelled"


class FraudSeverity(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class FraudStatus(str, enum.Enum):
    open = "open"
    resolved = "resolved"
    ignored = "ignored"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(160))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.borrower)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    profile: Mapped["BorrowerProfile"] = relationship(back_populates="user", uselist=False)
    applications: Mapped[list["LoanApplication"]] = relationship(back_populates="user")
    loans: Mapped[list["Loan"]] = relationship(back_populates="user")
    document_photos: Mapped[list["DocumentPhoto"]] = relationship(back_populates="user")
    locations: Mapped[list["LocationRecord"]] = relationship(back_populates="user")


class BorrowerProfile(Base):
    __tablename__ = "borrower_profiles"
    __table_args__ = (
        UniqueConstraint("national_id", name="uq_profile_national_id"),
        UniqueConstraint("phone", name="uq_profile_phone"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), unique=True, index=True)
    national_id: Mapped[str] = mapped_column(String(20))
    phone: Mapped[str] = mapped_column(String(20))
    notification_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    date_of_birth: Mapped[str] = mapped_column(String(20))
    county: Mapped[str] = mapped_column(String(60))
    sub_county: Mapped[str] = mapped_column(String(60))
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    location_landmark: Mapped[str | None] = mapped_column(String(200), nullable=True)
    employment_status: Mapped[str] = mapped_column(String(60), default="unknown")
    monthly_income: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    mpesa_monthly_inflow: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    kra_pin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    mpesa_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    residential_address: Mapped[str | None] = mapped_column(String(200), nullable=True)
    next_of_kin_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    next_of_kin_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    id_front_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    id_back_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    selfie_image_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    selfie_liveness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_selfie_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    business_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    business_age_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    business_photo_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payment_proof_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    payment_proof_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    device_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_id_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_location_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped[User] = relationship(back_populates="profile")


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    consent_type: Mapped[str] = mapped_column(String(80))
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    ip_address: Mapped[str | None] = mapped_column(String(80), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)


class LoanApplication(Base):
    __tablename__ = "loan_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    product_type: Mapped[ProductType] = mapped_column(Enum(ProductType))
    requested_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    term_days: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(200))
    status: Mapped[ApplicationStatus] = mapped_column(Enum(ApplicationStatus), default=ApplicationStatus.pending)
    risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_band: Mapped[str | None] = mapped_column(String(10), nullable=True)
    recommended_limit: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped[User] = relationship(back_populates="applications")
    offer: Mapped["LoanOffer"] = relationship(back_populates="application", uselist=False)


class LoanOffer(Base):
    __tablename__ = "loan_offers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    application_id: Mapped[str] = mapped_column(String(36), ForeignKey("loan_applications.id"), unique=True)
    principal_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    term_days: Mapped[int] = mapped_column(Integer)
    monthly_interest_rate: Mapped[float] = mapped_column(Numeric(5, 4))
    processing_fee_rate: Mapped[float] = mapped_column(Numeric(5, 4))
    processing_fee_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    interest_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    late_fee_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    total_due: Mapped[float] = mapped_column(Numeric(12, 2))
    duplum_cap_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    status: Mapped[OfferStatus] = mapped_column(Enum(OfferStatus), default=OfferStatus.offered)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    application: Mapped[LoanApplication] = relationship(back_populates="offer")


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    application_id: Mapped[str] = mapped_column(String(36), ForeignKey("loan_applications.id"), unique=True)
    offer_id: Mapped[str] = mapped_column(String(36), ForeignKey("loan_offers.id"), unique=True)
    product_type: Mapped[ProductType] = mapped_column(Enum(ProductType))
    principal_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    term_days: Mapped[int] = mapped_column(Integer)
    monthly_interest_rate: Mapped[float] = mapped_column(Numeric(5, 4))
    processing_fee_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    interest_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    late_fee_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    total_due: Mapped[float] = mapped_column(Numeric(12, 2))
    outstanding_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    duplum_cap_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    status: Mapped[LoanStatus] = mapped_column(Enum(LoanStatus), default=LoanStatus.pending_disbursement)
    disbursed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    late_interest_days_applied: Mapped[int] = mapped_column(Integer, default=0)
    reminder_10d_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped[User] = relationship(back_populates="loans")
    repayments: Mapped[list["Repayment"]] = relationship(back_populates="loan")
    document_photos: Mapped[list["DocumentPhoto"]] = relationship(back_populates="loan")


class Repayment(Base):
    __tablename__ = "repayments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    loan_id: Mapped[str] = mapped_column(String(36), ForeignKey("loans.id"), index=True)
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    channel: Mapped[str] = mapped_column(String(30), default="mpesa")
    paid_to_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reference: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    loan: Mapped[Loan] = relationship(back_populates="repayments")


class DocumentPhoto(Base):
    __tablename__ = "documents_photos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    loan_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("loans.id"), nullable=True, index=True)
    doc_type: Mapped[str] = mapped_column(String(40))
    photo_url: Mapped[str] = mapped_column(String(512))
    provider: Mapped[str] = mapped_column(String(30), default="cloudinary")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    user: Mapped[User] = relationship(back_populates="document_photos")
    loan: Mapped[Loan | None] = relationship(back_populates="document_photos")


class LocationRecord(Base):
    __tablename__ = "locations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    user: Mapped[User] = relationship(back_populates="locations")


class FraudFlag(Base):
    __tablename__ = "fraud_flags"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    application_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("loan_applications.id"), nullable=True)
    severity: Mapped[FraudSeverity] = mapped_column(Enum(FraudSeverity), default=FraudSeverity.low)
    reason: Mapped[str] = mapped_column(String(255))
    status: Mapped[FraudStatus] = mapped_column(Enum(FraudStatus), default=FraudStatus.open)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
