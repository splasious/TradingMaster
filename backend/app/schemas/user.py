from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str
    roles: list[str] = Field(default_factory=lambda: ["viewer"])


class UserUpdateRoles(BaseModel):
    roles: list[str]


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    is_active: bool
    roles: list[str]

    model_config = {"from_attributes": True}
