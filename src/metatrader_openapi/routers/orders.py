from fastapi import APIRouter, HTTPException, Request, Path, Body, Query
from typing import Any, Dict, List, Optional, Union

router = APIRouter()

# --- Pending Orders Endpoints ---
@router.get("/pending", response_model=List[Dict[str, Any]])
async def pending_all(request: Request):
    """Get all pending orders as a DataFrame.

    Input:
        None

    Response:
        List[Dict[str, Any]]: List of pending order records.
    """
    client = request.app.state.client
    try:
        df = client.order.get_all_pending_orders()
        return df.to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/pending/symbol/{symbol}", response_model=List[Dict[str, Any]])
async def pending_by_symbol(request: Request, symbol: str = Path(..., description="Symbol name")):
    """Get pending orders filtered by symbol.

    Input:
        symbol (str): Symbol name.

    Response:
        List[Dict[str, Any]]: List of pending order records for the symbol.
    """
    client = request.app.state.client
    try:
        df = client.order.get_pending_orders_by_symbol(symbol=symbol)
        return df.to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/pending/{id}", response_model=List[Dict[str, Any]])
async def pending_by_id(request: Request, id: Union[int, str] = Path(..., description="Order ID")):
    """Get pending order by ticket or ID.

    Input:
        id (Union[int, str]): Order ID.

    Response:
        List[Dict[str, Any]]: List of pending order records matching the ID.
    """
    client = request.app.state.client
    try:
        df = client.order.get_pending_orders_by_id(id=id)
        return df.to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/pending/{id}", response_model=Dict[str, Any])
async def modify_pending(
    request: Request,
    id: Union[int, str] = Path(..., description="Pending order ID"),
    price: Optional[float] = Body(None, description="New price"),
    stop_loss: Optional[float] = Body(None, description="Stop loss price"),
    take_profit: Optional[float] = Body(None, description="Take profit price"),
    strategy_id: Optional[str] = Body(None, description="Only modify the order if it belongs to this strategy"),
):
    """Modifies an existing pending order's price, stop loss, or take profit.

    Input:
        id (Union[int, str]): Pending order ID.
        price (Optional[float]): New price.
        stop_loss (Optional[float]): Stop loss price.
        take_profit (Optional[float]): Take profit price.

    Response:
        Dict[str, Any]: Modified order data.
    """
    client = request.app.state.client
    try:
        return client.order.modify_pending_order(id=id, price=price, stop_loss=stop_loss, take_profit=take_profit, strategy_id=strategy_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/pending/{id}", response_model=Dict[str, Any])
async def cancel_pending(
    request: Request,
    id: Union[int, str] = Path(..., description="Pending order ID"),
    strategy_id: Optional[str] = Query(None, description="Only cancel the order if it belongs to this strategy"),
):
    """Cancels a specific pending order by ID.

    Input:
        id (Union[int, str]): Pending order ID.

    Response:
        Dict[str, Any]: Cancellation result.
    """
    client = request.app.state.client
    try:
        return client.order.cancel_pending_order(id=id, strategy_id=strategy_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/pending", response_model=Dict[str, Any])
async def cancel_all_pending(
    request: Request,
    scope: Optional[str] = Query(None, description="'strategy', 'manual' or 'all' (default: all)"),
    strategy_id: Optional[str] = Query(None, description="Strategy for scope 'strategy'"),
    dry_run: bool = Query(False, description="Only list the affected items; nothing is sent"),
):
    """Cancels all pending orders in your account.

    Input:
        None

    Response:
        Dict[str, Any]: Cancellation result.
    """
    client = request.app.state.client
    try:
        return client.order.cancel_all_pending_orders(scope=scope, strategy_id=strategy_id, dry_run=dry_run)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/pending/symbol/{symbol}", response_model=Dict[str, Any])
async def cancel_pending_by_symbol(
    request: Request,
    symbol: str = Path(..., description="Symbol name"),
    scope: Optional[str] = Query(None, description="'strategy', 'manual' or 'all' (default: all)"),
    strategy_id: Optional[str] = Query(None, description="Strategy for scope 'strategy'"),
    dry_run: bool = Query(False, description="Only list the affected items; nothing is sent"),
):
    """Cancels all pending orders for a specific symbol.

    Input:
        symbol (str): Symbol name.

    Response:
        Dict[str, Any]: Cancellation result.
    """
    client = request.app.state.client
    try:
        return client.order.cancel_pending_orders_by_symbol(symbol=symbol, scope=scope, strategy_id=strategy_id, dry_run=dry_run)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- New Order Placement ---
@router.post("/market", response_model=Dict[str, Any])
async def place_market(
    request: Request,
    symbol: str = Body(..., description="Symbol name"),
    volume: float = Body(..., description="Lot size"),
    type: str = Body(..., description="Order type, 'BUY' or 'SELL'"),
    stop_loss: Optional[float] = Body(0.0, description="Stop loss price"),
    take_profit: Optional[float] = Body(0.0, description="Take profit price"),
    request_id: Optional[str] = Body(None, description="Stable ID of this intention; a repeated ID never sends again"),
    strategy_id: Optional[str] = Body(None, description="Strategy owning the order (magic number and strategy limits)"),
    preview_id: Optional[str] = Body(None, description="ID returned by /order/preview"),
):
    """Places a market order (BUY or SELL) for a specified financial instrument.

    Input:
        symbol (str): Symbol name.
        volume (float): Lot size.
        type (str): Order type, 'BUY' or 'SELL'.
        stop_loss (Optional[float]): Stop loss price.
        take_profit (Optional[float]): Take profit price.

    Response:
        Dict[str, Any]: Placed order data.
    """
    client = request.app.state.client
    try:
        return client.order.place_market_order(symbol=symbol, volume=volume, type=type, stop_loss=stop_loss, take_profit=take_profit,
                                               request_id=request_id, strategy_id=strategy_id, preview_id=preview_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/pending", response_model=Dict[str, Any])
async def place_pending(
    request: Request,
    symbol: str = Body(..., description="Symbol name"),
    volume: float = Body(..., description="Lot size"),
    type: str = Body(..., description="Order type, 'BUY' or 'SELL'"),
    price: float = Body(..., description="Pending order price"),
    stop_loss: Optional[float] = Body(0.0, description="Stop loss price"),
    take_profit: Optional[float] = Body(0.0, description="Take profit price"),
    request_id: Optional[str] = Body(None, description="Stable ID of this intention; a repeated ID never sends again"),
    strategy_id: Optional[str] = Body(None, description="Strategy owning the order (magic number and strategy limits)"),
    preview_id: Optional[str] = Body(None, description="ID returned by /order/preview"),
):
    """Place a pending order.

    Input:
        symbol (str): Symbol name.
        volume (float): Lot size.
        type (str): Order type, 'BUY' or 'SELL'.
        price (float): Pending order price.
        stop_loss (Optional[float]): Stop loss price.
        take_profit (Optional[float]): Take profit price.

    Response:
        Dict[str, Any]: Placed pending order data.
    """
    client = request.app.state.client
    try:
        return client.order.place_pending_order(symbol=symbol, volume=volume, type=type, price=price, stop_loss=stop_loss, take_profit=take_profit,
                                                request_id=request_id, strategy_id=strategy_id, preview_id=preview_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Pre-trade checks, request identity and limits ---
def _call(request: Request, function):
    client = request.app.state.client
    try:
        return function(client)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/snapshot/{symbol}", response_model=Dict[str, Any])
async def trading_snapshot(request: Request, symbol: str = Path(..., description="Symbol name"),
                           strategy_id: Optional[str] = Query(None, description="Strategy for ownership and limits")):
    """Quote, account, positions, pending orders and risk limits of a symbol in one call (successive reads, not atomic)."""
    return _call(request, lambda client: client.order.get_trading_snapshot(symbol, strategy_id=strategy_id))


@router.post("/preview", response_model=Dict[str, Any])
async def preview(
    request: Request,
    symbol: str = Body(..., description="Symbol name"),
    type: str = Body(..., description="BUY or SELL (pending when price is given) or an explicit pending type"),
    volume: Optional[float] = Body(None, description="Lot size (or pass risk_amount)"),
    risk_amount: Optional[float] = Body(None, description="Maximum loss at the stop loss, account currency"),
    price: Optional[float] = Body(None, description="Pending order price"),
    stop_loss: Optional[float] = Body(None, description="Stop loss price"),
    take_profit: Optional[float] = Body(None, description="Take profit price"),
    strategy_id: Optional[str] = Body(None, description="Strategy owning the order"),
):
    """Check an order without sending it; returns a preview_id to send it with."""
    return _call(request, lambda client: client.order.preview_order(
        symbol=symbol, type=type, volume=volume, risk_amount=risk_amount, price=price,
        stop_loss=stop_loss, take_profit=take_profit, strategy_id=strategy_id,
    ))


@router.post("/calculate/profit", response_model=Dict[str, Any])
async def calculate_profit(
    request: Request,
    symbol: str = Body(...), type: str = Body(...), volume: float = Body(...),
    entry_price: float = Body(...), exit_price: float = Body(...),
):
    """Estimated profit/loss in the account currency (order_calc_profit)."""
    return _call(request, lambda client: client.order.calculate_trade_outcome(
        symbol=symbol, type=type, volume=volume, entry_price=entry_price, exit_price=exit_price))


@router.post("/calculate/margin", response_model=Dict[str, Any])
async def calculate_margin(
    request: Request,
    symbol: str = Body(...), type: str = Body(...), volume: float = Body(...), price: Optional[float] = Body(None),
):
    """Estimated margin in the account currency (order_calc_margin)."""
    return _call(request, lambda client: client.order.calculate_required_margin(symbol=symbol, type=type, volume=volume, price=price))


@router.get("/requests", response_model=Dict[str, Any])
async def requests_list(request: Request, state: Optional[str] = Query(None), limit: int = Query(50)):
    """Most recent tracked trade requests."""
    return _call(request, lambda client: client.order.list_requests(state=state, limit=limit))


@router.get("/requests/{request_id}", response_model=Dict[str, Any])
async def request_status(request: Request, request_id: str = Path(...)):
    """Recorded state and events of a request_id."""
    return _call(request, lambda client: client.order.get_request_status(request_id))


@router.post("/requests/{request_id}/reconcile", response_model=Dict[str, Any])
async def request_reconcile(request: Request, request_id: str = Path(...)):
    """Search the terminal for the effects of an uncertain request and update its state (never sends)."""
    return _call(request, lambda client: client.order.reconcile_request(request_id))


@router.post("/requests/{request_id}/resolve", response_model=Dict[str, Any])
async def request_resolve(request: Request, request_id: str = Path(...),
                          decision: str = Body(..., description="not_executed or accept_unknown"),
                          reason: str = Body(..., description="Why the decision is taken (audited)")):
    """Explicit, audited decision for a request whose outcome stays unknown."""
    return _call(request, lambda client: client.order.resolve_request(request_id, decision=decision, reason=reason))


@router.get("/limits", response_model=Dict[str, Any])
async def risk_limits(request: Request):
    """Effective risk limits, new entries switch and their change history."""
    return _call(request, lambda client: client.order.get_risk_limits())


@router.post("/entries/disable", response_model=Dict[str, Any])
async def entries_disable(request: Request, reason: str = Body(..., embed=True)):
    """Block new openings; reducing exposure stays available."""
    return _call(request, lambda client: client.order.disable_new_entries(reason=reason, source="rest"))


@router.post("/entries/enable", response_model=Dict[str, Any])
async def entries_enable(request: Request, reason: str = Body(..., embed=True)):
    """Allow new openings again (operator action; the configuration switch still applies)."""
    return _call(request, lambda client: client.order.enable_new_entries(reason=reason, source="rest"))
