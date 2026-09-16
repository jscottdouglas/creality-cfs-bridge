"""One rotating log file, so `serve` can run for months unattended.

The bridge is started as a Windows service with no console attached, so
everything it has to say has to land in a file. `~/.cfsbridge/serve.log`, five
files of one megabyte each, is the whole policy.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Optional

LOGGER_NAME = "cfsbridge"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 5

_configured = False


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def setup_logging(path: Optional[str] = None, level: int = logging.INFO,
                  to_stream: bool = True) -> logging.Logger:
    """Configure the package logger once. Returns it.

    A path that cannot be opened is reported on stderr and then ignored; a
    missing log file must never stop the bridge from serving.
    """
    global _configured
    logger = get_logger()
    logger.setLevel(level)
    if _configured:
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    if to_stream:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        logger.addHandler(stream)

    if path:
        target = os.path.expanduser(path)
        try:
            directory = os.path.dirname(target)
            if directory:
                os.makedirs(directory, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                target, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(fmt)
            logger.addHandler(handler)
        except OSError as exc:
            print("cfsbridge: cannot write the log at %s: %s" % (target, exc),
                  file=sys.stderr)

    logger.propagate = False
    _configured = True
    return logger
