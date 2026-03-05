from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal
from app.models import (
    ApplicationStatus,
    BorrowerProfile,
    Consent,
    Loan,
    LoanApplication,
    LoanOffer,
    LoanStatus,
    OfferStatus,
    ProductType,
    User,
    UserRole,
)
from app.security import hash_password

REQUIRED_CONSENTS = [
    "privacy_policy",
    "loan_terms",
    "data_processing",
    "credit_assessment",
    "collection_policy",
]


def _seed_users() -> list[dict[str, str]]:
    return [
        {"full_name": "Demo Borrower One", "email": "demo1@aflex.loans", "phone": "+254711000001"},
        {"full_name": "Demo Borrower Two", "email": "demo2@aflex.loans", "phone": "+254711000002"},
        {"full_name": "Demo Borrower Three", "email": "demo3@aflex.loans", "phone": "+254711000003"},
        {"full_name": "Demo Borrower Four", "email": "demo4@aflex.loans", "phone": "+254711000004"},
        {"full_name": "Demo Borrower Five", "email": "demo5@aflex.loans", "phone": "+254711000005"},
    ]


def _ensure_user(db, row: dict[str, str]) -> User:
    user = db.query(User).filter(User.phone == row["phone"]).first()
    if user:
        return user
    user = User(
        full_name=row["full_name"],
        email=row["email"],
        phone=row["phone"],
        password_hash=hash_password("DemoPass123!"),
        role=UserRole.borrower,
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user


def _ensure_profile(db, user: User, idx: int) -> None:
    profile = db.query(BorrowerProfile).filter(BorrowerProfile.user_id == user.id).first()
    if profile:
        return
    profile = BorrowerProfile(
        user_id=user.id,
        national_id=f"1234567{idx}",
        phone=user.phone or "",
        notification_email=user.email,
        date_of_birth="1995-01-01",
        county="Nairobi",
        sub_county="Westlands",
        gps_lat=-1.286389,
        gps_lng=36.817223,
        location_accuracy_m=35,
        location_captured_at=datetime.now(UTC),
        location_landmark="Nairobi CBD",
        employment_status="self_employed",
        monthly_income=45000,
        mpesa_monthly_inflow=50000,
        mpesa_phone=user.phone,
        residential_address="Nairobi, Kenya",
        next_of_kin_name=f"Kin {idx}",
        next_of_kin_phone="+254722000000",
        is_id_verified=True,
        is_location_verified=True,
    )
    db.add(profile)


def _ensure_consents(db, user: User) -> None:
    existing = {c.consent_type for c in db.query(Consent).filter(Consent.user_id == user.id).all()}
    for consent_type in REQUIRED_CONSENTS:
        if consent_type in existing:
            continue
        db.add(
            Consent(
                user_id=user.id,
                consent_type=consent_type,
                accepted=True,
                accepted_at=datetime.now(UTC),
                ip_address="127.0.0.1",
                user_agent="seed-script",
            )
        )


def _ensure_sample_loan(db, user: User, idx: int) -> None:
    has_loan = db.query(Loan).filter(Loan.user_id == user.id).first()
    if has_loan:
        return

    requested = 3000 + (idx * 1500)
    app = LoanApplication(
        user_id=user.id,
        product_type=ProductType.personal,
        requested_amount=requested,
        term_days=30,
        purpose="Business top-up",
        status=ApplicationStatus.reviewed,
        risk_score=72,
        risk_band="A",
        recommended_limit=requested + 2000,
    )
    db.add(app)
    db.flush()

    principal = float(requested)
    interest = round(principal * 0.18, 2)
    processing = round(principal * 0.03, 2)
    total_due = round(principal + interest + processing, 2)
    offer = LoanOffer(
        application_id=app.id,
        principal_amount=principal,
        term_days=30,
        monthly_interest_rate=0.18,
        processing_fee_rate=0.03,
        processing_fee_amount=processing,
        interest_amount=interest,
        late_fee_amount=150,
        total_due=total_due,
        duplum_cap_amount=principal * 2,
        status=OfferStatus.accepted,
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )
    db.add(offer)
    db.flush()

    db.add(
        Loan(
            user_id=user.id,
            application_id=app.id,
            offer_id=offer.id,
            product_type=ProductType.personal,
            principal_amount=principal,
            term_days=30,
            monthly_interest_rate=0.18,
            processing_fee_amount=processing,
            interest_amount=interest,
            late_fee_amount=150,
            total_due=total_due,
            outstanding_amount=total_due,
            duplum_cap_amount=principal * 2,
            status=LoanStatus.active,
            disbursed_at=datetime.now(UTC),
            due_at=datetime.now(UTC) + timedelta(days=30),
        )
    )


def main() -> None:
    db = SessionLocal()
    try:
        users = []
        for idx, row in enumerate(_seed_users(), start=1):
            user = _ensure_user(db, row)
            _ensure_profile(db, user, idx)
            _ensure_consents(db, user)
            if idx <= 3:
                _ensure_sample_loan(db, user, idx)
            users.append(user)
        db.commit()
        print(f"Seed complete. Demo users prepared: {len(users)}")
        print("Default demo password: DemoPass123!")
    finally:
        db.close()


if __name__ == "__main__":
    main()
