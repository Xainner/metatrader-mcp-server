import argparse
import json
import logging
import os
from pathlib import Path
import sys

from .runtime import load, process_batch, run_processor, status


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Forex-gold research adapter; no execution')
    parser.add_argument('--home', type=Path)
    parser.add_argument('command', choices=['init', 'status', 'process-once', 'processor', 'mcp', 'analyze'])
    args = parser.parse_args()
    if args.command == 'mcp':
        from .mcp_server import serve
        serve(args.home)
        return
    if args.command == 'processor':
        run_processor(args.home)
        return
    _, config, research, inbox = load(args.home)
    if args.command == 'analyze':
        logging.basicConfig(handlers=[logging.NullHandler()], force=True)
        data = sys.stdin.buffer.read(1_000_001)
        if len(data) > 1_000_000:
            raise ValueError('Context exceeds maximum size')
        print(json.dumps(research.analyze(json.loads(data)), allow_nan=False))
        return
    if args.command == 'init':
        inbox.initialize()
    if args.command == 'process-once':
        process_batch(config, research, inbox)
    print(json.dumps(status(config, inbox), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
