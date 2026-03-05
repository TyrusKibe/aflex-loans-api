from dataclasses import dataclass

from .models import ProductType


@dataclass
class RiskResult:
    score: int
    band: str
    approved: bool
    max_offer_amount: float
    pricing_multiplier: float
    reasons: list[str]


def compute_risk_score(
    *,
    product_type: ProductType,
    requested_amount: float,
    monthly_income: float,
    mpesa_monthly_inflow: float,
    business_age_months: int | None,
    is_id_verified: bool,
    is_location_verified: bool,
    has_active_loan: bool,
    has_open_fraud_flags: bool,
) -> RiskResult:
    reasons: list[str] = []
    score = 50

    if is_id_verified:
        score += 15
    else:
        reasons.append("ID not verified")

    if is_location_verified:
        score += 10
    else:
        reasons.append("Location not verified")

    inflow_proxy = max(monthly_income, mpesa_monthly_inflow)
    if inflow_proxy >= requested_amount * 2.5:
        score += 12
    elif inflow_proxy >= requested_amount * 1.5:
        score += 6
    else:
        score -= 12
        reasons.append("Low repayment capacity versus requested amount")

    if product_type == ProductType.business:
        if business_age_months is None:
            score -= 10
            reasons.append("Business age not provided")
        elif business_age_months >= 24:
            score += 10
        elif business_age_months >= 6:
            score += 4
        else:
            score -= 8
            reasons.append("Business too new for requested risk")

    if has_active_loan:
        score -= 40
        reasons.append("Existing active loan detected")

    if has_open_fraud_flags:
        score -= 60
        reasons.append("Open fraud risk flag")

    score = max(0, min(100, score))

    if score >= 85:
        band = "A"
        multiplier = 1.0
    elif score >= 75:
        band = "B"
        multiplier = 0.9
    elif score >= 65:
        band = "C"
        multiplier = 0.75
    elif score >= 55:
        band = "D"
        multiplier = 0.5
    else:
        band = "E"
        multiplier = 0.0

    approved = score >= 58 and not has_open_fraud_flags and not has_active_loan
    max_offer_amount = max(0.0, requested_amount * multiplier)
    if not approved:
        max_offer_amount = 0.0

    return RiskResult(
        score=score,
        band=band,
        approved=approved,
        max_offer_amount=round(max_offer_amount, 2),
        pricing_multiplier=multiplier if multiplier > 0 else 1.0,
        reasons=reasons if reasons else ["Profile meets baseline criteria"],
    )

