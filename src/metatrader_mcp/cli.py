import click
import os
from dotenv import load_dotenv
from metatrader_mcp.server import mcp
from metatrader_mcp.logging_config import configure_logging
from metatrader_mcp.utils import resolve_transport_config, run_mcp

# Load .env before click resolves options from environment variables.
load_dotenv()

@click.command()
@click.option("--login", required=True, type=int, envvar="LOGIN", help="MT5 login ID (env: LOGIN)")
@click.option("--password", required=True, envvar="PASSWORD", help="MT5 password (env: PASSWORD, preferred over the command line)")
@click.option("--server", required=True, envvar="SERVER", help="MT5 server name (env: SERVER)")
@click.option("--path", default=None, envvar="MT5_PATH", help="Path to MT5 terminal executable (optional, auto-detected if not provided)")
@click.option("--transport", default=None, type=click.Choice(["sse", "stdio", "streamable-http"], case_sensitive=False), help="MCP transport type (default: stdio, env: MCP_TRANSPORT)")
@click.option("--host", default=None, help="Host to bind for SSE/HTTP transport (default: 127.0.0.1, env: MCP_HOST). Other hosts require MCP_AUTH_TOKEN and TLS")
@click.option("--port", default=None, type=int, help="Port to bind for SSE/HTTP transport (default: 8080, env: MCP_PORT)")
@click.option("--ssl-certfile", default=None, help="TLS certificate file for SSE/HTTP transport (env: MCP_SSL_CERTFILE)")
@click.option("--ssl-keyfile", default=None, help="TLS private key file for SSE/HTTP transport (env: MCP_SSL_KEYFILE)")
def main(login, password, server, path, transport, host, port, ssl_certfile, ssl_keyfile):
    """Launch the MetaTrader MCP server."""
    # override env vars if provided via CLI
    os.environ["login"] = str(login)
    os.environ["password"] = password
    os.environ["server"] = server
    if path:
        os.environ["MT5_PATH"] = path

    # Logs go to a rotating file: stdout carries JSON-RPC and stderr may not be read.
    configure_logging()
    transport, host, port = resolve_transport_config(transport, host, port)
    try:
        run_mcp(mcp, transport, host, port, ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)
    except ValueError as exc:
        raise click.ClickException(str(exc))

if __name__ == "__main__":
    # pylint: disable=no-value-for-parameter
    main()
