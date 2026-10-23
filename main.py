import argparse
import logging
from pathlib import Path

from services.pipeline_flow_service import generate_flows

def main() -> None:
    parser = argparse.ArgumentParser(description='Generate offline OSB flow diagrams.')
    parser.add_argument(
        '--root', type=Path, required=True,
        help='Recursively discover local projects by .jpr files under this directory.',
    )
    parser.add_argument(
        '--output', type=Path, default=Path.home() / 'osb-diagrams',
        help='Directory for generated Draw.io files (default: ~/osb-diagrams).',
    )
    args = parser.parse_args()
    project_dir = Path(__file__).resolve().parent
    log_dir = project_dir / 'logs'
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / 'pipeline-flow.log'),
        ],
    )
    generate_flows(args.root, args.output.expanduser())
if __name__ == "__main__":
    main()
