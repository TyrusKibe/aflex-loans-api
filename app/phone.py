def normalize_phone_number(value: str) -> str:
    raw = (value or "").strip()
    digits = "".join(char for char in raw if char.isdigit())
    if not digits:
        return ""

    # Normalize Kenyan variants into one canonical E.164 format (+254XXXXXXXXX).
    if digits.startswith("2540") and len(digits) == 13:
        digits = f"254{digits[4:]}"
    elif digits.startswith("0") and len(digits) == 10:
        digits = f"254{digits[1:]}"
    elif len(digits) == 9:
        digits = f"254{digits}"

    if digits.startswith("254") and len(digits) == 12:
        return f"+{digits}"
    if raw.startswith("+"):
        return f"+{digits}"
    if digits.startswith("254"):
        return f"+{digits}"
    return f"+{digits}"


def is_valid_kenyan_phone(value: str) -> bool:
    normalized = normalize_phone_number(value)
    digits = "".join(char for char in normalized if char.isdigit())
    return digits.startswith("254") and len(digits) == 12
