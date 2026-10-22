import logging
import os
from typing import Any, Optional, Union
from metatrader_client import client
from metatrader_mcp.security import BearerTokenMiddleware, check_network_security, env_flag, is_loopback

logger = logging.getLogger(__name__)

DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


def resolve_transport_config(transport=None, host=None, port=None):
	"""Resolve transport config: CLI flag > env var > default (local STDIO)."""
	transport = (transport or os.getenv("MCP_TRANSPORT") or DEFAULT_TRANSPORT).lower()
	host = host or os.getenv("MCP_HOST") or DEFAULT_HOST
	port = port if port is not None else int(os.getenv("MCP_PORT") or DEFAULT_PORT)
	return transport, host, port


def build_http_app(mcp, transport, host, *, token=None):
	"""
	Build the ASGI app for the SSE or streamable HTTP transport.

	DNS rebinding protection stays enabled for loopback hosts. For other hosts
	the allowed Host headers can be listed in MCP_ALLOWED_HOSTS (comma separated);
	requests are authenticated with the bearer token in any case.
	"""
	from mcp.server.transport_security import TransportSecuritySettings

	allowed_hosts = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
	if allowed_hosts:
		mcp.settings.transport_security = TransportSecuritySettings(
			enable_dns_rebinding_protection=True,
			allowed_hosts=allowed_hosts,
		)
	elif not is_loopback(host):
		# Host headers of remote clients are unknown; the bearer token is the access control.
		mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

	if transport == "sse":
		app = mcp.sse_app()
	elif transport == "streamable-http":
		app = mcp.streamable_http_app()
	else:
		raise ValueError(f"Unsupported transport {transport}")
	return BearerTokenMiddleware(app, token) if token else app


def run_mcp(mcp, transport, host, port, *, ssl_certfile=None, ssl_keyfile=None, allow_insecure_remote=None):
	"""
	Run the MCP server with the resolved transport config.

	STDIO needs no network access. SSE and streamable HTTP listen on ``host``;
	any non-loopback host requires MCP_AUTH_TOKEN and TLS (see metatrader_mcp.security).
	"""
	if transport == "stdio":
		mcp.run(transport="stdio")
		return

	token = os.getenv("MCP_AUTH_TOKEN") or None
	ssl_certfile = ssl_certfile or os.getenv("MCP_SSL_CERTFILE") or None
	ssl_keyfile = ssl_keyfile or os.getenv("MCP_SSL_KEYFILE") or None
	if allow_insecure_remote is None:
		allow_insecure_remote = env_flag("MCP_ALLOW_INSECURE_REMOTE")
	check_network_security(
		host,
		token=token,
		ssl_certfile=ssl_certfile,
		ssl_keyfile=ssl_keyfile,
		allow_insecure_remote=allow_insecure_remote,
	)

	import uvicorn

	mcp.settings.host = host
	mcp.settings.port = port
	app = build_http_app(mcp, transport, host, token=token)
	if not token:
		logger.warning("MCP %s server on %s:%s has no authentication (MCP_AUTH_TOKEN not set)", transport, host, port)
	uvicorn.run(
		app,
		host=host,
		port=port,
		ssl_certfile=ssl_certfile,
		ssl_keyfile=ssl_keyfile,
		log_level=mcp.settings.log_level.lower(),
	)

def init(
	login: Optional[Union[str, int]],
	password: Optional[str],
	server: Optional[str],
	path: Optional[str] = None,
	*,
	raise_on_failure: bool = True,
) -> Optional[client.MT5Client]:
	"""
	Initialize MT5Client

	Args:
		login (Optional[Union[str, int]]): Login ID
		password (Optional[str]): Password
		server (Optional[str]): Server name
		path (Optional[str]): Path to MT5 terminal executable (default: None for auto-detect)
		raise_on_failure (bool): Raise when the first connection fails (default). With False the
			client is returned unconnected and the error is logged, so a caller with a
			reconnection policy (the MCP bridge) can retry later.

	Returns:
		Optional[client.MT5Client]: MT5Client instance if all parameters are provided, None otherwise
	"""

	if login and password and server:
		config = {
			"login": int(login),
			"password": password,
			"server": server,
		}

		# Add path to config if provided
		if path:
			config["path"] = path

		mt5_client = client.MT5Client(config=config)
		try:
			mt5_client.connect()
		except Exception as exc:
			if raise_on_failure:
				raise
			logger.warning("Initial connection to MetaTrader 5 failed, will retry on demand: %s", exc)
		return mt5_client

	return None

def get_client(ctx: Any) -> client.MT5Client:
	mt5_client = ctx.request_context.lifespan_context.client
	if mt5_client is None:
		raise RuntimeError("MetaTrader 5 is not configured: provide login, password and server")
	return mt5_client
