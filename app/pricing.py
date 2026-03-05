from dataclasses import dataclass

from .config import Settings
from .models import ProductType


@dataclass
class PricingResult:
    principal: float
    term_days: int
    monthly_interest_rate: float
    processing_fee_rate: float
    processing_fee_amount: float
    interest_amount: float
    late_fee_amount: float
    total_due: float
    duplum_cap_amount: float


def _risk_spread_factor(risk_band: str) -> float:
    mapping = {
        "A": 0.0,
        "B": 0.2,
        "C": 0.45,
        "D": 0.7,
        "E": 1.0,
    }
    return mapping.get(risk_band.upper(), 0.5)


def compute_pricing(
    *,
    settings: Settings,
    product_type: ProductType,
    principal: float,
    term_days: int,
    risk_band: str,
) -> PricingResult:
    spread = _risk_spread_factor(risk_band)

    if product_type == ProductType.personal:
        # Personal model: borrower repays approved principal by day 30.
        # No monthly interest or processing fee is charged upfront;
        # late interest is applied separately after due date.
        monthly_interest = 0.0
        processing_rate = 0.0
        late_fee = 0.0
    else:
        interest_min = settings.business_monthly_interest_min
        interest_max = settings.business_monthly_interest_max
        late_fee = settings.business_late_fee
        monthly_interest = interest_min + ((interest_max - interest_min) * spread)
        processing_rate = settings.processing_fee_min + (
            (settings.processing_fee_max - settings.processing_fee_min) * spread
        )

    month_factor = term_days / 30.0
    interest_amount = principal * monthly_interest * month_factor
    processing_fee_amount = principal * processing_rate
    raw_total_due = principal + interest_amount + processing_fee_amount

    # Duplum-inspired hard cap: total amount recoverable should not exceed 2x principal.
    duplum_cap = principal * 2.0
    total_due = min(raw_total_due, duplum_cap)

    # If capped by duplum, reduce interest portion first while preserving processing fee transparency.
    if raw_total_due > duplum_cap:
        interest_amount = max(0.0, duplum_cap - principal - processing_fee_amount)

    return PricingResult(
        principal=round(principal, 2),
        term_days=term_days,
        monthly_interest_rate=round(monthly_interest, 4),
        processing_fee_rate=round(processing_rate, 4),
        processing_fee_amount=round(processing_fee_amount, 2),
        interest_amount=round(interest_amount, 2),
        late_fee_amount=round(late_fee, 2),
        total_due=round(total_due, 2),
        duplum_cap_amount=round(duplum_cap, 2),
    )
