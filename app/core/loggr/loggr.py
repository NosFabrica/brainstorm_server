import logging
import sys

from app.core.loggr.formatter import ColoredFormatter

log_format = "%(white)s%(asctime)s - %(cyan)s%(name)s - %(log_color)s%(levelname)s - %(log_color)s%(message)s"  # noqa: E501
formatter = ColoredFormatter(
    fmt=log_format,
    log_colors={
        "DEBUG": "light_green",
        "INFO": "light_white",
        "WARNING": "light_yellow",
        "ERROR": "light_red",
        "CRITICAL": "red,bg_white",
    },
)


def get_console_handler():
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    return console_handler


def get_logger(logger_name):
    logger = logging.getLogger(logger_name)

    logger.setLevel(logging.INFO)

    logger.addHandler(get_console_handler())

    # with this pattern, it's rarely necessary to propagate the error up to parent
    logger.propagate = False

    return logger
