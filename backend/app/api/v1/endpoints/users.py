import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import require_role
from app.core.security import hash_password
from app.db.session import get_db
from app.models.user import Role, User, UserRole
from app.schemas.user import UserApprove, UserCreate, UserOut, UserUpdateRoles
from app.services.audit import write_audit_log

router = APIRouter()


def _to_out(user: User) -> UserOut:
    return UserOut(
        id=str(user.id), email=user.email, full_name=user.full_name,
        is_active=user.is_active, is_approved=user.is_approved, roles=user.role_names,
    )


@router.get("", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db), admin: User = Depends(require_role("administrator"))
) -> list[UserOut]:
    result = await db.execute(select(User).options(selectinload(User.user_roles).selectinload(UserRole.role)))
    return [_to_out(u) for u in result.scalars().all()]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("administrator")),
) -> UserOut:
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    roles_result = await db.execute(select(Role).where(Role.name.in_(payload.roles)))
    roles = roles_result.scalars().all()
    if len(roles) != len(set(payload.roles)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more roles do not exist")

    user = User(email=payload.email, hashed_password=hash_password(payload.password), full_name=payload.full_name)
    user.user_roles = [UserRole(role=role) for role in roles]
    db.add(user)
    await db.flush()

    await write_audit_log(
        db,
        user_id=admin.id,
        action="USER_CREATED",
        object_type="user",
        object_id=str(user.id),
        new_value={"email": user.email, "roles": payload.roles},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(user, attribute_names=["user_roles"])
    return _to_out(user)


@router.post("/{user_id}/approve", response_model=UserOut)
async def approve_user(
    user_id: str,
    payload: UserApprove,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("administrator")),
) -> UserOut:
    result = await db.execute(
        select(User)
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .where(User.id == uuid.UUID(user_id))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.is_approved:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is already approved")

    roles_result = await db.execute(select(Role).where(Role.name.in_(payload.roles)))
    roles = roles_result.scalars().all()
    if len(roles) != len(set(payload.roles)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more roles do not exist")

    user.is_approved = True
    user.user_roles = [UserRole(role=role) for role in roles]
    await db.flush()

    await write_audit_log(
        db,
        user_id=admin.id,
        action="USER_APPROVED",
        object_type="user",
        object_id=str(user.id),
        new_value={"email": user.email, "roles": payload.roles},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(user, attribute_names=["user_roles"])
    return _to_out(user)


@router.post("/{user_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject_user(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("administrator")),
) -> None:
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.is_approved:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot reject an already-approved user"
        )

    await write_audit_log(
        db,
        user_id=admin.id,
        action="USER_REJECTED",
        object_type="user",
        object_id=str(user.id),
        previous_value={"email": user.email},
        ip_address=request.client.host if request.client else None,
    )
    await db.delete(user)
    await db.commit()


@router.put("/{user_id}/roles", response_model=UserOut)
async def update_user_roles(
    user_id: str,
    payload: UserUpdateRoles,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("administrator")),
) -> UserOut:
    result = await db.execute(
        select(User)
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .where(User.id == uuid.UUID(user_id))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    roles_result = await db.execute(select(Role).where(Role.name.in_(payload.roles)))
    roles = roles_result.scalars().all()
    if len(roles) != len(set(payload.roles)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more roles do not exist")

    previous_roles = user.role_names
    user.user_roles = [UserRole(role=role) for role in roles]
    await db.flush()

    await write_audit_log(
        db,
        user_id=admin.id,
        action="USER_ROLES_UPDATED",
        object_type="user",
        object_id=str(user.id),
        previous_value={"roles": previous_roles},
        new_value={"roles": payload.roles},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(user, attribute_names=["user_roles"])
    return _to_out(user)
