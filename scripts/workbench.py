"""Start the local production workbench using this project's Python environment."""
from argparse import ArgumentParser
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = ArgumentParser(description='Local story-video workbench')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--workspace', type=Path, default=ROOT,
                        help='Separate fixture workspace for offline validation')
    args = parser.parse_args()
    import uvicorn
    from workbench.app import create_app
    uvicorn.run(create_app(args.workspace), host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
