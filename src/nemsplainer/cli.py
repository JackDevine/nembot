"""One-shot command-line and TOML interface for monthly MMS archives."""
import argparse
from pathlib import Path
import sys
import tomllib

import duckdb

from .ingest import load_mms, validate_inputs
from .tables import TABLES


def main() -> None:
    parser = argparse.ArgumentParser(description='Load monthly AEMO MMS archives into a persistent DuckDB database. Defaults to the latest three published months.')
    parser.add_argument('--tables', nargs='+', default=argparse.SUPPRESS, metavar='NAME')
    parser.add_argument('--start', default=argparse.SUPPRESS, metavar='YYYY-MM-DD')
    parser.add_argument('--end', default=argparse.SUPPRESS, metavar='YYYY-MM-DD')
    parser.add_argument('--database', default=argparse.SUPPRESS, metavar='PATH')
    parser.add_argument('--cache-dir', default=argparse.SUPPRESS, metavar='PATH')
    parser.add_argument('--config', type=Path, metavar='PATH')
    args = vars(parser.parse_args())
    config_path = args.pop('config')
    options = dict(tables=list(TABLES), start=None, end=None, database='data/nem.duckdb', cache_dir='data/cache')
    try:
        if config_path is not None:
            with config_path.open('rb') as source:
                config = tomllib.load(source)
            unknown = config.keys() - options.keys()
            if unknown:
                raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
            # Validate even overridden config values rather than hide bad config.
            candidate = options | config
            validate_inputs(candidate['tables'], candidate['start'], candidate['end'], database=candidate['database'], cache_dir=candidate['cache_dir'])
            options.update(config)
        options.update(args)
        names, start, end, database, cache = validate_inputs(options['tables'], options['start'], options['end'], database=options['database'], cache_dir=options['cache_dir'])
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    try:
        connection = load_mms(names, start, end, database=database, cache_dir=cache)
        connection.close()
    except KeyboardInterrupt:
        print('Interrupted; committed files remain resumable.', file=sys.stderr)
        raise SystemExit(130) from None
    except (OSError, RuntimeError, ValueError, duckdb.Error) as exc:
        print(f'nemsplainer: {exc}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
