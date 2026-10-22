"""Versioned XAUUSDm demo deployment: native 0.7 tools plus research adapters.

MIT for this deployment; loaded research functions retain their AGPL license.
Never start this module with a real account. No rules or orders are created on startup.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import functools
import importlib.util
from importlib.metadata import version
import inspect
import json
import os
from pathlib import Path
import sys
import threading

import MetaTrader5 as mt5
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import ToolAnnotations
from metatrader_client import MT5Client
from metatrader_mcp import server as upstream
from metatrader_mcp.bridge import Bridge
from metatrader_mcp.logging_config import configure_logging

ROOT = Path(os.environ.get('MT5_DEMO_HOME', Path(__file__).resolve().parents[3]))
CORE_COMMIT = '207496cb29111720c0eefaf613982a5febac0863'
SYMBOL = 'XAUUSDm'
LOCK = threading.RLock()
BOUND_ACCOUNT = None
REAL_ORDER_SEND = mt5.order_send


def configuration():
    """Load private deployment settings, retaining environment risk limits."""
    path = ROOT/'demo-config.json'
    config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    config.setdefault('terminal_path', r'C:\Program Files\MetaTrader 5\terminal64.exe')
    config.setdefault('state_db', str(ROOT/'data/state.sqlite3'))
    config.setdefault('supervisor_enabled', True)
    config.setdefault('duplicate_window_seconds', 3.0)
    if config['duplicate_window_seconds'] != 3.0:
        raise ValueError('This deployment preserves the owner-selected 3-second duplicate window')
    return config


def ensure_demo(*, writing=False):
    """Refuse disconnected terminals, live accounts and account changes."""
    account, terminal = mt5.account_info(), mt5.terminal_info()
    if account is None or terminal is None or not terminal.connected:
        raise RuntimeError('MT5 no está conectado a una cuenta demo')
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise RuntimeError('Solo se permiten cuentas DEMO')
    if BOUND_ACCOUNT is not None and (account.login, account.server) != BOUND_ACCOUNT:
        raise RuntimeError('La cuenta MT5 cambió; reconecta el MCP para vincularla de nuevo')
    if writing and (not terminal.trade_allowed or terminal.tradeapi_disabled or
                    not account.trade_allowed or not account.trade_expert):
        raise RuntimeError('MT5/cuenta bloquea trading algorítmico o la API Python')
    return account, terminal


def checked_order_send(request):
    """Check demo binding and permissions at the last native send boundary."""
    with LOCK:
        ensure_demo(writing=True)
        return REAL_ORDER_SEND(request)


class DemoBridge(Bridge):
    def _call(self, tool, stage, function, ticket):
        def guarded():
            with LOCK:
                ensure_demo()
                return function()
        return super()._call(tool, stage, guarded, ticket)


@asynccontextmanager
async def lifespan(server):
    global BOUND_ACCOUNT
    config = configuration()
    client = None
    bridge = None
    supervisor = None
    if not mt5.initialize(config['terminal_path'], timeout=15000):
        raise RuntimeError('No se pudo conectar con el terminal MT5')
    try:
        account, _ = ensure_demo()
        BOUND_ACCOUNT = (account.login, account.server)
        client = MT5Client(config={
            'path': config['terminal_path'], 'state_db': config['state_db'],
            'risk': {'duplicate_window_seconds': 3.0, 'allow_live_trading': False},
        })
        client.connect()
        ensure_demo()
        mt5.order_send = checked_order_send
        bridge = DemoBridge(client)
        if config['supervisor_enabled']:
            from metatrader_client.supervisor import PositionSupervisor
            supervisor = PositionSupervisor(client.connection)
            loop = asyncio.get_running_loop()
            supervisor.start(lambda cycle: asyncio.run_coroutine_threadsafe(
                bridge.run('position_supervisor', 'send', cycle), loop).result())
        yield upstream.AppContext(client=client, bridge=bridge, supervisor=supervisor)
    finally:
        if supervisor is not None:
            await asyncio.to_thread(supervisor.stop)
        if bridge is not None:
            bridge.shutdown()
        mt5.order_send = REAL_ORDER_SEND
        BOUND_ACCOUNT = None
        if client is not None:
            client.disconnect()
        else:
            mt5.shutdown()


mcp = FastMCP('metatrader-demo', lifespan=lifespan, log_level='WARNING', instructions=(
    'Centro XAUUSDm, solo cuenta DEMO vinculada al iniciar. Primero demo_status. '
    'Herramientas nativas 0.7: snapshots y previews devuelven error/message/data; '
    'preview_order requiere symbol="XAUUSDm" y type="BUY"/"SELL", con volume o risk_amount. '
    'Usa send_with.arguments con preview_id y un request_id estable por intención. '
    'No reintentes resultados inciertos: consulta get_request_status/reconcile_request y posiciones. '
    'El riesgo/reservas coordina conexiones que usan el mismo state_db. No hay presupuestos automáticos elegidos. '
    'Bulk requiere scope explícito; primero dry_run. Crear una regla autoriza al supervisor a ejecutarla: '
    'hazlo solo bajo mandato del propietario. No se crean reglas ni se abre nada al arrancar. '
    'get_gold_research captura datos reales de MT5 y analiza en server2; no reconstruyas ni renueves timestamps. '
    'Las señales de WhatsApp/Telegram se consultan por gold-research; no son permisos de ejecución. '
    'La cuenta personal FXTradingVision sigue pendiente de vinculación; no afirmar recepción. '
    'Las pruebas de envío de 0.7 en terminal demo siguen pendientes; esta instalación se verifica sin órdenes.'
))

WRITE_TOOLS = {
    'place_market_order','place_pending_order','modify_position','modify_pending_order',
    'close_position','cancel_pending_order','close_all_positions','close_all_positions_by_symbol',
    'close_all_profitable_positions','close_all_losing_positions','cancel_all_pending_orders',
    'cancel_pending_orders_by_symbol','resolve_request','disable_new_entries','create_position_rule',
    'pause_position_rule','resume_position_rule','remove_position_rule',
}

# Retain the upstream functions and schemas. Each MT5 call goes through DemoBridge.
for tool in upstream.mcp._tool_manager.list_tools():
    mcp.add_tool(tool.fn, name=tool.name, description=tool.description,
                 annotations=ToolAnnotations(readOnlyHint=tool.name not in WRITE_TOOLS,
                                             destructiveHint=tool.name in WRITE_TOOLS),
                 structured_output=False)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def demo_status(ctx: Context) -> dict:
    """Read demo binding, deployment version, durable state and execution validation limits."""
    app = ctx.request_context.lifespan_context
    def read():
        account, terminal = ensure_demo()
        return {
            'account_is_demo': True, 'connected': terminal.connected,
            'ready_to_trade': bool(terminal.trade_allowed and not terminal.tradeapi_disabled and
                                   account.trade_allowed and account.trade_expert),
            'algorithmic_trading_enabled': terminal.trade_allowed,
            'python_trading_disabled': terminal.tradeapi_disabled,
            'package_version': version('metatrader-mcp-server'), 'installed_commit': CORE_COMMIT,
            'requested_symbol': SYMBOL, 'transport': 'stdio',
            'market_order_sl_tp_supported': True, 'pending_order_preserves_sl_tp': True,
            'upstream_fixes_pending': False, 'demo_execution_validation_pending': True,
            'duplicate_window_seconds': app.client.connection.risk_guard.config.duplicate_window_seconds,
            'reservation_ttl_seconds': 60,
            'risk_guard_scope': 'connections_and_processes_sharing_state_db_and_account',
            'state_database': str(app.client.connection.store.path),
            'supervisor_running': app.supervisor is not None,
            'research_tools': sorted(RESEARCH_TOOLS),
            'numeric_risk_limits': app.client.connection.risk_guard.config.as_dict(),
        }
    return await app.bridge.run('demo_status','read',read)


class Collector:
    def tool(self, **kwargs):
        return lambda function: function


_research_path = Path(__file__).resolve().parents[1]/'gold-research/client/windows_tools.py'
_spec = importlib.util.spec_from_file_location('demo_research_07', _research_path)
research = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(research)
legacy = research.register(Collector(), mt5, LOCK, ensure_demo)
RESEARCH_TOOLS = {'get_symbol_specs','get_market_context','calculate_trade_risk','get_gold_research'}


def bridged_research(name, function):
    @functools.wraps(function)
    async def call(ctx: Context, *args, **kwargs):
        app = ctx.request_context.lifespan_context
        return await app.bridge.run(name, 'read', lambda: function(*args, **kwargs))
    call.__signature__ = inspect.Signature([
        inspect.Parameter('ctx', inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context),
        *inspect.signature(function).parameters.values()], return_annotation=dict)
    # FastMCP detects injected Context through annotations, before reading the signature.
    call.__annotations__ = {**getattr(function, '__annotations__', {}), 'ctx': Context}
    return call


for name in sorted(RESEARCH_TOOLS - {'get_gold_research'}):
    mcp.add_tool(bridged_research(name,legacy[name]),name=name,
                 annotations=ToolAnnotations(readOnlyHint=True),structured_output=False)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,openWorldHint=True))
async def get_gold_research(ctx: Context, count: int = 160) -> dict:
    """Capture live XAUUSDm via the bridge, then analyze by SSH outside the MT5 worker."""
    app = ctx.request_context.lifespan_context
    context = await app.bridge.run('get_market_context','read',lambda: legacy['get_market_context'](count=count))
    return await asyncio.to_thread(research.analyze_market_context, context)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    os.environ['MCP_LOG_STDERR_LEVEL'] = 'off'
    os.environ.setdefault('MCP_LOG_FILE', str(ROOT/'logs/mcp-0.7.log'))
    os.environ.setdefault('MCP_BUILD_COMMIT',CORE_COMMIT)
    configure_logging()
    if args.check:
        from types import SimpleNamespace
        async def check():
            async with lifespan(mcp) as app:
                ctx=SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))
                print(json.dumps(await demo_status(ctx),indent=2))
        asyncio.run(check())
    else:
        mcp.run(transport='stdio')


if __name__ == '__main__':
    main()
