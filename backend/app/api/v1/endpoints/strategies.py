import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.schemas.strategy import StrategyCreate, StrategyOut, StrategyVersionCreate, StrategyVersionOut, ValidateResult
from app.services.audit import write_audit_log
from app.services.strategy.rules import evaluate_rule_node
from app.services.strategy.sandbox import run_python_strategy
from app.services.strategy.state_machine import StrategyStatus, can_transition

router = APIRouter()

_SAMPLE_CANDLES = [
    {"open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i, "volume": 1000.0} for i in range(30)
]


def _version_out(version: StrategyVersion) -> StrategyVersionOut:
    return StrategyVersionOut(
        id=str(version.id), version_number=version.version_number, timeframe=version.timeframe,
        instrument_ids=version.instrument_ids, parameters=version.parameters, entry_rules=version.entry_rules,
        exit_rules=version.exit_rules, python_code=version.python_code, position_sizing=version.position_sizing,
        risk_rules=version.risk_rules, created_at=version.created_at,
    )


def _strategy_out(strategy: Strategy) -> StrategyOut:
    latest = strategy.versions[-1] if strategy.versions else None
    return StrategyOut(
        id=str(strategy.id), name=strategy.name, description=strategy.description, code_type=strategy.code_type,
        status=strategy.status, owner_id=str(strategy.owner_id), created_at=strategy.created_at,
        updated_at=strategy.updated_at, latest_version=_version_out(latest) if latest else None,
    )


async def _load_strategy(db: AsyncSession, strategy_id: str) -> Strategy:
    result = await db.execute(
        select(Strategy).options(selectinload(Strategy.versions)).where(Strategy.id == uuid.UUID(strategy_id))
    )
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    return strategy


def _assert_can_edit(strategy: Strategy, user: User) -> None:
    if strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")


@router.post("", response_model=StrategyOut, status_code=status.HTTP_201_CREATED)
async def create_strategy(
    payload: StrategyCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader", "analyst")),
) -> StrategyOut:
    code_type = "python" if payload.version.python_code else "visual"
    strategy = Strategy(name=payload.name, description=payload.description, owner_id=user.id, code_type=code_type)
    db.add(strategy)
    await db.flush()

    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe=payload.version.timeframe,
        instrument_ids=payload.version.instrument_ids, parameters=payload.version.parameters,
        entry_rules=payload.version.entry_rules, exit_rules=payload.version.exit_rules,
        python_code=payload.version.python_code, position_sizing=payload.version.position_sizing.model_dump(),
        risk_rules=payload.version.risk_rules.model_dump(), created_by=user.id,
    )
    db.add(version)

    await write_audit_log(
        db, user_id=user.id, action="STRATEGY_CREATED", object_type="strategy", object_id=str(strategy.id),
        new_value={"name": strategy.name, "code_type": code_type},
    )
    await db.commit()
    return StrategyOut(
        id=str(strategy.id), name=strategy.name, description=strategy.description, code_type=strategy.code_type,
        status=strategy.status, owner_id=str(strategy.owner_id), created_at=strategy.created_at,
        updated_at=strategy.updated_at, latest_version=_version_out(version),
    )


@router.get("", response_model=list[StrategyOut])
async def list_strategies(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> list[StrategyOut]:
    stmt = select(Strategy).options(selectinload(Strategy.versions))
    if "administrator" not in user.role_names:
        stmt = stmt.where(Strategy.owner_id == user.id)
    result = await db.execute(stmt.order_by(Strategy.created_at.desc()))
    return [_strategy_out(s) for s in result.scalars().all()]


@router.get("/{strategy_id}", response_model=StrategyOut)
async def get_strategy(
    strategy_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> StrategyOut:
    strategy = await _load_strategy(db, strategy_id)
    if strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")
    return _strategy_out(strategy)


@router.post("/{strategy_id}/versions", response_model=StrategyOut)
async def create_strategy_version(
    strategy_id: str, payload: StrategyVersionCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> StrategyOut:
    strategy = await _load_strategy(db, strategy_id)
    _assert_can_edit(strategy, user)

    next_number = max((v.version_number for v in strategy.versions), default=0) + 1
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=next_number, timeframe=payload.timeframe,
        instrument_ids=payload.instrument_ids, parameters=payload.parameters, entry_rules=payload.entry_rules,
        exit_rules=payload.exit_rules, python_code=payload.python_code,
        position_sizing=payload.position_sizing.model_dump(), risk_rules=payload.risk_rules.model_dump(),
        created_by=user.id,
    )
    db.add(version)
    strategy.code_type = "python" if payload.python_code else "visual"
    # A code change invalidates any prior backtest/paper-trading progress
    # (PRD section 25's pipeline is a real pipeline).
    strategy.status = StrategyStatus.DRAFT.value

    await write_audit_log(
        db, user_id=user.id, action="STRATEGY_VERSION_CREATED", object_type="strategy", object_id=str(strategy.id),
        new_value={"version_number": next_number},
    )
    await db.commit()
    # "updated_at" is server-computed (onupdate=func.now()) so the commit
    # expired it specifically; refresh it alongside the relationship or
    # accessing it below triggers a lazy load outside of an async context.
    await db.refresh(strategy, attribute_names=["versions", "updated_at", "status"])
    return _strategy_out(strategy)


@router.post("/{strategy_id}/validate", response_model=ValidateResult)
async def validate_strategy(
    strategy_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> ValidateResult:
    """Dry-run validation (PRD section 15's Validation step): syntax +
    sandboxed execution for Python strategies, rule-tree evaluation for
    visual ones. Uses the strategy's own recent candles if it names an
    instrument with backfilled data, else a small synthetic sample --
    either way this never touches real risk/order-placement paths."""
    strategy = await _load_strategy(db, strategy_id)
    if strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")

    version = strategy.versions[-1]
    sample = _SAMPLE_CANDLES
    if version.instrument_ids:
        result = await db.execute(
            select(OhlcvCandle)
            .where(OhlcvCandle.instrument_id == uuid.UUID(version.instrument_ids[0]), OhlcvCandle.timeframe == version.timeframe)
            .order_by(OhlcvCandle.ts.desc())
            .limit(60)
        )
        candles = list(reversed(result.scalars().all()))
        if candles:
            sample = [{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume} for c in candles]

    if version.python_code:
        sandbox_result = await run_python_strategy(version.python_code, sample, version.parameters)
        return ValidateResult(valid=sandbox_result.error is None, error=sandbox_result.error, sample_signal=sandbox_result.signal)

    try:
        instrument = await db.get(Instrument, uuid.UUID(version.instrument_ids[0])) if version.instrument_ids else None
        candles_for_rules = (
            (
                await db.execute(
                    select(OhlcvCandle)
                    .where(OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == version.timeframe)
                    .order_by(OhlcvCandle.ts)
                )
            ).scalars().all()
            if instrument
            else []
        )
        entry_match = evaluate_rule_node(candles_for_rules, version.entry_rules) if candles_for_rules else False
        return ValidateResult(valid=True, sample_signal="BUY" if entry_match else "HOLD")
    except ValueError as exc:
        return ValidateResult(valid=False, error=str(exc))


async def _transition_strategy(
    strategy_id: str, target: StrategyStatus, action_name: str, db: AsyncSession, user: User
) -> StrategyOut:
    """Shared by mark-validated/approve: both are explicit human judgment
    calls (PRD section 15's "User Approval" step, section 25's pipeline) --
    there's no automatic trigger for either, unlike BACKTESTED or
    PAPER_TRADING which are earned by real backtest/paper-trading runs."""
    strategy = await _load_strategy(db, strategy_id)
    _assert_can_edit(strategy, user)

    current = StrategyStatus(strategy.status)
    if not can_transition(current, target):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot move from '{current.value}' to '{target.value}'",
        )

    previous = strategy.status
    strategy.status = target.value
    await write_audit_log(
        db, user_id=user.id, action=action_name, object_type="strategy", object_id=str(strategy.id),
        previous_value={"status": previous}, new_value={"status": target.value},
    )
    await db.commit()
    # "updated_at" is server-computed (onupdate=func.now()) so commit
    # expires it specifically -- must be included here too (see the same
    # fix in create_strategy_version above) or _strategy_out's access to it
    # triggers a lazy load outside an async context.
    await db.refresh(strategy, attribute_names=["versions", "updated_at", "status"])
    return _strategy_out(strategy)


@router.post("/{strategy_id}/mark-validated", response_model=StrategyOut)
async def mark_strategy_validated(
    strategy_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> StrategyOut:
    return await _transition_strategy(strategy_id, StrategyStatus.VALIDATED, "STRATEGY_MARKED_VALIDATED", db, user)


@router.post("/{strategy_id}/approve", response_model=StrategyOut)
async def approve_strategy(
    strategy_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator", "trader"))
) -> StrategyOut:
    return await _transition_strategy(strategy_id, StrategyStatus.APPROVED, "STRATEGY_APPROVED", db, user)
