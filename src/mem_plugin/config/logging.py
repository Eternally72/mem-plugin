"""Process-owned logging configuration for the HTTP service."""

import logging
import os
import re
import sys

FORMAT = "[%(name)s] %(asctime)s %(levelname)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
LINE_PREFIX = re.compile(r"^(\[[^\]\n]+\]) (\S+) ([A-Z]+) ")
EVENT = re.compile(r"^[a-z][a-z0-9_]*(?=\s|=|$)")
LEVELS = {"DEBUG": 90, "INFO": 32, "WARNING": 33, "ERROR": 31, "CRITICAL": 91}
ROLES = {"api": 36, "worker": 35, "scheduler": 34}


def terminal_line(line: str, *, role: str | None = None) -> str:
    prefix = f"[{role}] " if role else ""
    if not sys.stderr.isatty() or "NO_COLOR" in os.environ or os.getenv("TERM") == "dumb":
        return prefix + line

    def color(text, code):
        return f"\x1b[{code}m{text}\x1b[0m"

    if role:
        prefix = color(f"[{role}]", ROLES.get(role, 36)) + " "
    match = LINE_PREFIX.match(line)
    if not match:
        return prefix + line
    logger, timestamp, level = match.groups()
    message = line[match.end():]
    event = EVENT.match(message)
    if event:
        message = color(event[0], 36) + message[event.end():]
    severity = color(level, LEVELS.get(level, 37))
    return f"{prefix}{color(logger, 94)} {color(timestamp, 90)} {severity} {message}"


class TerminalFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return terminal_line(super().format(record))


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(TerminalFormatter(FORMAT, datefmt=DATE_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
