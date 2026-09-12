from pydantic import BaseModel, EmailStr, Field, field_validator


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str
    roles: list[str] = Field(default_factory=lambda: ["viewer"])

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        # See LoginRequest's identical validator (schemas/auth.py) for why:
        # normalizing every email-carrying schema the same way makes
        # lookups case-insensitive by construction, not by patching each
        # query individually.
        return v.strip().lower()


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class UserUpdateRoles(BaseModel):
    roles: list[str]


class UserUpdate(BaseModel):
    full_name: str | None = None
    roles: list[str] | None = None
    is_active: bool | None = None


class UserApprove(BaseModel):
    roles: list[str] = Field(default_factory=lambda: ["viewer"])


class ForgotPasswordRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class AdminResetPassword(BaseModel):
    new_password: str = Field(min_length=8)


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    is_active: bool
    is_approved: bool
    password_reset_requested: bool
    roles: list[str]

    model_config = {"from_attributes": True}
