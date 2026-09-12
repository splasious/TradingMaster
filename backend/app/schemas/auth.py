from pydantic import BaseModel, EmailStr, field_validator


class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        # Real production bug this fixes: a user typed "Amibroker9768@..."
        # (capital A, as browser autofill commonly does) against a stored
        # "amibroker9768@..." row -- Postgres text equality is case-
        # sensitive, so the correct password was rejected as "Invalid
        # email or password" with no way for the user to tell why.
        # Normalizing at every entry point (this, UserRegister, UserCreate,
        # ForgotPasswordRequest) makes email lookups case-insensitive by
        # construction rather than patching each query individually.
        return v.strip().lower()


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
