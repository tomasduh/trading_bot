import logging
from logging.handlers import RotatingFileHandler
import colorlog
from src.config import LOG_DIR

# 5 MB por archivo, mantén 3 archivos rotados (15 MB total máx)
MAX_LOG_SIZE_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT   = 3


def setup(name: str = "bot") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    fmt = "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s"
    console = colorlog.StreamHandler()
    console.setFormatter(colorlog.ColoredFormatter(fmt, datefmt="%H:%M:%S"))
    console.setLevel(logging.INFO)

    file_handler = RotatingFileHandler(
        LOG_DIR / "bot.log",
        maxBytes=MAX_LOG_SIZE_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    file_handler.setLevel(logging.DEBUG)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger
