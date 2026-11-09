"""Run the local-only AstroLearner voice studio."""

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="AstroLearner TTS · estudio local")
    parser.add_argument("--runtime", type=Path, help="Carpeta local de modelos, voces y audios")
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("El puerto debe estar entre 1 y 65535.")
    import uvicorn

    from .server import create_app

    uvicorn.run(create_app(args.runtime, port=args.port), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
