# OSB Diagram Generator

Generate Draw.io diagrams from local Oracle Service Bus (OSB) project
configuration. The generator reads files locally; it does not connect to OSB or
clone repositories.

## Requirements

- Python 3.12 or newer
- A local directory containing OSB projects

## Usage

Run the generator with the directory to scan:

```bash
python3 main.py --root /path/to/osb-projects
```

Projects are discovered recursively by their `.jpr` files. To choose an output
directory, use `--output`:

```bash
python3 main.py --root /path/to/osb-projects --output /path/to/diagrams
```

By default, diagrams are written to `~/osb-diagrams/`. Each project with proxy
services gets a Draw.io file and SVG exports. The Draw.io file contains an
overview and a detailed pipeline flow.

The generator analyzes configuration only. It does not execute transformations
or evaluate runtime conditions. Missing or unsupported references are marked
in the diagrams.

Use a root containing `.jpr` project files. There is no repository-list file or
project-copying step.

## Tests

Run the test suite with Python 3.12 or newer:

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
