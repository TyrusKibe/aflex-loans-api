"""initial schema

Revision ID: 20260305_000001
Revises:
Create Date: 2026-03-05 18:40:00
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260305_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=160), nullable=False),
        sa.Column("role", sa.Enum("borrower", "admin", name="userrole"), nullable=False, server_default="borrower"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("phone"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_phone", "users", ["phone"], unique=True)

    op.create_table(
        "borrower_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("national_id", sa.String(length=20), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("notification_email", sa.String(length=255), nullable=True),
        sa.Column("date_of_birth", sa.String(length=20), nullable=False),
        sa.Column("county", sa.String(length=60), nullable=False),
        sa.Column("sub_county", sa.String(length=60), nullable=False),
        sa.Column("gps_lat", sa.Float(), nullable=True),
        sa.Column("gps_lng", sa.Float(), nullable=True),
        sa.Column("location_accuracy_m", sa.Float(), nullable=True),
        sa.Column("location_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("location_landmark", sa.String(length=200), nullable=True),
        sa.Column("employment_status", sa.String(length=60), nullable=False),
        sa.Column("monthly_income", sa.Numeric(12, 2), nullable=False),
        sa.Column("mpesa_monthly_inflow", sa.Numeric(12, 2), nullable=False),
        sa.Column("kra_pin", sa.String(length=20), nullable=True),
        sa.Column("mpesa_phone", sa.String(length=20), nullable=True),
        sa.Column("residential_address", sa.String(length=200), nullable=True),
        sa.Column("next_of_kin_name", sa.String(length=160), nullable=True),
        sa.Column("next_of_kin_phone", sa.String(length=20), nullable=True),
        sa.Column("id_front_hash", sa.String(length=128), nullable=True),
        sa.Column("id_back_hash", sa.String(length=128), nullable=True),
        sa.Column("selfie_image_hash", sa.String(length=128), nullable=True),
        sa.Column("selfie_liveness_score", sa.Float(), nullable=True),
        sa.Column("is_selfie_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("business_name", sa.String(length=200), nullable=True),
        sa.Column("business_age_months", sa.Integer(), nullable=True),
        sa.Column("business_photo_hash", sa.String(length=128), nullable=True),
        sa.Column("payment_proof_type", sa.String(length=40), nullable=True),
        sa.Column("payment_proof_hash", sa.String(length=128), nullable=True),
        sa.Column("device_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("is_id_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_location_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("national_id", name="uq_profile_national_id"),
        sa.UniqueConstraint("phone", name="uq_profile_phone"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index("ix_borrower_profiles_user_id", "borrower_profiles", ["user_id"], unique=True)

    op.create_table(
        "consents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("consent_type", sa.String(length=80), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_address", sa.String(length=80), nullable=True),
        sa.Column("user_agent", sa.String(length=400), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_consents_user_id", "consents", ["user_id"], unique=False)

    op.create_table(
        "loan_applications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("product_type", sa.Enum("personal", "business", name="producttype"), nullable=False),
        sa.Column("requested_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("term_days", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "reviewed", "approved", "rejected", name="applicationstatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("risk_score", sa.Integer(), nullable=True),
        sa.Column("risk_band", sa.String(length=10), nullable=True),
        sa.Column("recommended_limit", sa.Numeric(12, 2), nullable=True),
        sa.Column("rejection_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_loan_applications_user_id", "loan_applications", ["user_id"], unique=False)

    op.create_table(
        "loan_offers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("principal_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("term_days", sa.Integer(), nullable=False),
        sa.Column("monthly_interest_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("processing_fee_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("processing_fee_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("interest_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("late_fee_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("total_due", sa.Numeric(12, 2), nullable=False),
        sa.Column("duplum_cap_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "status",
            sa.Enum("offered", "accepted", "expired", "revoked", name="offerstatus"),
            nullable=False,
            server_default="offered",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["loan_applications.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id"),
    )

    op.create_table(
        "loans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("offer_id", sa.String(length=36), nullable=False),
        sa.Column("product_type", sa.Enum("personal", "business", name="producttype"), nullable=False),
        sa.Column("principal_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("term_days", sa.Integer(), nullable=False),
        sa.Column("monthly_interest_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("processing_fee_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("interest_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("late_fee_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("total_due", sa.Numeric(12, 2), nullable=False),
        sa.Column("outstanding_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("duplum_cap_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending_disbursement",
                "active",
                "repaid",
                "defaulted",
                "cancelled",
                name="loanstatus",
            ),
            nullable=False,
            server_default="pending_disbursement",
        ),
        sa.Column("disbursed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("late_interest_days_applied", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reminder_10d_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["loan_applications.id"]),
        sa.ForeignKeyConstraint(["offer_id"], ["loan_offers.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id"),
        sa.UniqueConstraint("offer_id"),
    )
    op.create_index("ix_loans_user_id", "loans", ["user_id"], unique=False)

    op.create_table(
        "repayments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("loan_id", sa.String(length=36), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column("paid_to_phone", sa.String(length=20), nullable=True),
        sa.Column("reference", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["loan_id"], ["loans.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_repayments_loan_id", "repayments", ["loan_id"], unique=False)

    op.create_table(
        "fraud_flags",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=True),
        sa.Column("severity", sa.Enum("low", "medium", "high", "critical", name="fraudseverity"), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("status", sa.Enum("open", "resolved", "ignored", name="fraudstatus"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["application_id"], ["loan_applications.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fraud_flags_user_id", "fraud_flags", ["user_id"], unique=False)

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"], unique=False)

    op.create_table(
        "documents_photos",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("loan_id", sa.String(length=36), nullable=True),
        sa.Column("doc_type", sa.String(length=40), nullable=False),
        sa.Column("photo_url", sa.String(length=512), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False, server_default="cloudinary"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["loan_id"], ["loans.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_photos_user_id", "documents_photos", ["user_id"], unique=False)
    op.create_index("ix_documents_photos_loan_id", "documents_photos", ["loan_id"], unique=False)

    op.create_table(
        "locations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("accuracy_m", sa.Float(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_locations_user_id", "locations", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_locations_user_id", table_name="locations")
    op.drop_table("locations")
    op.drop_index("ix_documents_photos_loan_id", table_name="documents_photos")
    op.drop_index("ix_documents_photos_user_id", table_name="documents_photos")
    op.drop_table("documents_photos")
    op.drop_index("ix_audit_logs_user_id", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_fraud_flags_user_id", table_name="fraud_flags")
    op.drop_table("fraud_flags")
    op.drop_index("ix_repayments_loan_id", table_name="repayments")
    op.drop_table("repayments")
    op.drop_index("ix_loans_user_id", table_name="loans")
    op.drop_table("loans")
    op.drop_table("loan_offers")
    op.drop_index("ix_loan_applications_user_id", table_name="loan_applications")
    op.drop_table("loan_applications")
    op.drop_index("ix_consents_user_id", table_name="consents")
    op.drop_table("consents")
    op.drop_index("ix_borrower_profiles_user_id", table_name="borrower_profiles")
    op.drop_table("borrower_profiles")
    op.drop_index("ix_users_phone", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    op.execute("DROP TYPE IF EXISTS fraudstatus")
    op.execute("DROP TYPE IF EXISTS fraudseverity")
    op.execute("DROP TYPE IF EXISTS loanstatus")
    op.execute("DROP TYPE IF EXISTS offerstatus")
    op.execute("DROP TYPE IF EXISTS applicationstatus")
    op.execute("DROP TYPE IF EXISTS producttype")
    op.execute("DROP TYPE IF EXISTS userrole")
