"""Logging goes to STDERR on purpose.

The MCP reporting server talks to the app over stdout (stdio transport). Any
stray print/log line on stdout would corrupt that channel, so we never log there.
Docker captures stderr exactly like stdout, so nothing is lost.
"""
import logging
import os
import sys

_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, _LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)

# Keep noisy libraries quiet; the Telegram URL contains the bot token.
for _noisy in ("httpx", "httpcore", "telegram", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("expense-tracker")
