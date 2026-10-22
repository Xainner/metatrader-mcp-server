from fastapi import APIRouter, Body, HTTPException, Path, Query, Request
from typing import Any, Dict, Optional

router = APIRouter()


def _call(request: Request, function):
    client = request.app.state.client
    try:
        return function(client)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("", response_model=Dict[str, Any])
async def create_rule(
    request: Request,
    operator: str = Body(..., description="'>=' or '<='"),
    threshold: float = Body(..., description="Threshold in the account currency"),
    ticket: Optional[int] = Body(None, description="Position ticket (or strategy_id)"),
    strategy_id: Optional[str] = Body(None, description="Strategy whose positions are watched"),
    symbol: Optional[str] = Body(None, description="Only this symbol (strategy rules)"),
    metric: str = Body("pnl", description="'pnl' (profit + swap) or 'profit'"),
):
    """Create a persistent position rule evaluated by the local supervisor."""
    return _call(request, lambda client: client.supervisor.create_position_rule(
        operator=operator, threshold=threshold, ticket=ticket, strategy_id=strategy_id, symbol=symbol, metric=metric))


@router.get("", response_model=Dict[str, Any])
async def list_rules(request: Request, state: Optional[str] = Query(None), include_events: bool = Query(False)):
    """Position rules and their state."""
    return _call(request, lambda client: client.supervisor.list_position_rules(state=state, include_events=include_events))


@router.post("/{rule_id}/pause", response_model=Dict[str, Any])
async def pause_rule(request: Request, rule_id: str = Path(...)):
    """Pause an active rule."""
    return _call(request, lambda client: client.supervisor.pause_position_rule(rule_id))


@router.post("/{rule_id}/resume", response_model=Dict[str, Any])
async def resume_rule(request: Request, rule_id: str = Path(...)):
    """Resume a paused rule."""
    return _call(request, lambda client: client.supervisor.resume_position_rule(rule_id))


@router.delete("/{rule_id}", response_model=Dict[str, Any])
async def remove_rule(request: Request, rule_id: str = Path(...)):
    """Remove a rule (kept as removed in the audit trail)."""
    return _call(request, lambda client: client.supervisor.remove_position_rule(rule_id))
