import logging

from .config import load_settings
from .server import build_server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()
    build_server(settings).run(transport="streamable-http")


if __name__ == "__main__":
    main()
