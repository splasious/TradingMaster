"""Global emergency stop (PRD sections 24.5, 49). A single-row table, read
before every live order and every live deployment start -- once active, no
new live order can be placed by anyone until an administrator deactivates
it explicitly.
"""

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.live_trading import KillSwitch


async def get_kill_switch(db: AsyncSession) -> KillSwitch:
    switch = await db.get(KillSwitch, 1)
    if switch is None:
        switch = KillSwitch(id=1, active=False)
        db.add(switch)
        await db.flush()
    return switch


async def activate(db: AsyncSession, user_id, reason: str) -> KillSwitch:
    switch = await get_kill_switch(db)
    switch.active = True
    switch.activated_by = user_id
    switch.activated_at = datetime.now(timezone.utc)
    switch.reason = reason
    return switch


async def deactivate(db: AsyncSession) -> KillSwitch:
    switch = await get_kill_switch(db)
    switch.active = False
    switch.activated_by = None
    switch.activated_at = None
    switch.reason = None
    return switch
