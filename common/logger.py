import logging
import os
from datetime import datetime


LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def setup_logger(log_dir=LOG_DIR, prefix="clean_read"):
    """Настройка логгера: вывод в консоль + файл в logs/ (в корне проекта)."""
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(
        log_dir, f"{prefix}_{datetime.now():%Y-%m-%d}.log"
    )

    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(name)-12s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    root.addHandler(fh)
    root.addHandler(ch)
