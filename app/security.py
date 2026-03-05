from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import settings

ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt", "pbkdf2_sha256"], deprecated="auto")


def _access_secret() -> str:
    return settings.access_token_secret


def _refresh_secret() -> str:
    return settings.refresh_token_secret


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(subject: str, expires_minutes: int | None = None) -> str:
    expiry = datetime.now(UTC) + timedelta(minutes=expires_minutes or settings.access_token_expire_minutes)
    payload = {"sub": subject, "exp": expiry, "type": "access"}
    return jwt.encode(payload, _access_secret(), algorithm=ALGORITHM)


def create_refresh_token(subject: str, expires_minutes: int | None = None) -> str:
    expiry = datetime.now(UTC) + timedelta(minutes=expires_minutes or settings.refresh_token_expire_minutes)
    payload = {"sub": subject, "exp": expiry, "type": "refresh"}
    return jwt.encode(payload, _refresh_secret(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, _access_secret(), algorithms=[ALGORITHM])
        if payload.get("type") != "access":
            return None
        return payload
    except JWTError:
        return None


def decode_refresh_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, _refresh_secret(), algorithms=[ALGORITHM])
        if payload.get("type") != "refresh":
            return None
        return payload
    except JWTError:
        return None
