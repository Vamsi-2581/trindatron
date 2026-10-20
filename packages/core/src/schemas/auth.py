from pydantic import BaseModel, EmailStr, field_validator
from datetime import datetime
from typing import Literal

#: A client that names itself gets a persistent session (short access token +
#: rotating refresh token). None keeps the legacy long-lived access token.
SessionClient = Literal["web", "desktop", "mobile"]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str = ""
    client: SessionClient | None = None

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        return validate_new_password(v)


def validate_new_password(v: str) -> str:
    """Every password a user chooses — signup, reset, change — passes this."""
    if len(v) < 8:
        raise ValueError("Password must be at least 8 characters")
    # bcrypt has a hard 72-byte limit and raises ValueError past it —
    # validated here (422, Pydantic's own clean error shape) so that
    # ValueError never reaches create_user()'s generic
    # "except ValueError: 409 (email already registered)" handler,
    # which would otherwise leak bcrypt's own library message under
    # the wrong status code (AUTH-13, Security Hardening).
    if len(v.encode("utf-8")) > 72:
        raise ValueError("Password must be at most 72 bytes")
    return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    client: SessionClient | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    #: Seconds until access_token expires (persistent sessions only).
    expires_in: int | None = None
    #: Returned in the body to desktop and mobile only. Web receives it as an
    #: httpOnly cookie and this stays null.
    refresh_token: str | None = None
    refresh_expires_at: datetime | None = None


class RefreshRequest(BaseModel):
    """Desktop/mobile send the token in the body; web sends nothing and the
    cookie is used."""
    refresh_token: str | None = None


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    plan: str
    is_active: bool
    # IANA timezone, or None if not yet detected (see PATCH /users/me/timezone).
    # The frontend uses this to skip a redundant PATCH when the browser's
    # detected value already matches what's stored.
    timezone: str | None = None
    created_at: datetime
    #: True once the owner proved the address (confirmation link, reset link or Google).
    email_verified: bool = False

    model_config = {"from_attributes": True}


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    password: str

    @field_validator("password")
    @classmethod
    def password_rules(cls, v: str) -> str:
        return validate_new_password(v)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_rules(cls, v: str) -> str:
        return validate_new_password(v)


class VerifyEmailRequest(BaseModel):
    token: str


class SecurityEventResponse(BaseModel):
    kind: str
    label: str
    method: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    created_at: datetime
