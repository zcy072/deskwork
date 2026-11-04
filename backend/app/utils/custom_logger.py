import contextvars
import logging
import logging.handlers
import os

trace_id_var = contextvars.ContextVar("trace_id", default="-")
session_id_var = contextvars.ContextVar("session_id", default="-")


class TraceIdFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = trace_id_var.get("-")
        record.session_id = session_id_var.get("-")
        return True


def get_trace_id() -> str:
    return trace_id_var.get("-")


def get_session_id() -> str:
    return session_id_var.get("-")


class ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[34m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record):
        levelname = record.levelname
        formatted = super().format(record)
        if levelname in self.COLORS:
            color = self.COLORS[levelname]
            return f"{color}{formatted}{self.RESET}"
        return formatted


LOG_FORMAT = (
    "[%(asctime)s] | trace=%(trace_id)s | session=%(session_id)s | %(levelname)-5s | "
    "%(name)s:%(lineno)d | %(message)s"
)
# 和 LOG_FILE 一样直接读环境变量(容器里由 compose 的 env_file 注入):logger 要在 settings 之前可用
LOG_FILE = os.getenv("LOG_FILE", "./app.log")  # 置空则只输出到终端
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUPS = 3


def get_logger(
    name: str | None = None,
    level: int | str = LOG_LEVEL,
    log_to_file: bool = True,
) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        trace_filter = TraceIdFilter()
        stream_handler = logging.StreamHandler()
        stream_handler.stream = open(
            stream_handler.stream.fileno(),
            mode="w",
            encoding="utf-8",
            errors="replace",
            closefd=False,
            buffering=1,
        )
        stream_handler.setFormatter(ColorFormatter(fmt=LOG_FORMAT))
        stream_handler.addFilter(trace_filter)
        logger.addHandler(stream_handler)
        if log_to_file and LOG_FILE:
            file_handler = logging.handlers.RotatingFileHandler(
                LOG_FILE,
                maxBytes=LOG_FILE_MAX_BYTES,
                backupCount=LOG_FILE_BACKUPS,
                encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter(fmt=LOG_FORMAT))
            file_handler.addFilter(trace_filter)
            logger.addHandler(file_handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


logger = get_logger(__name__)

if __name__ == "__main__":
    logger.debug("This is a debug message")
    logger.info("This is an info message")
    logger.warning("This is a warning message")
    logger.error("This is an error message")
    logger.critical("This is a critical message")
