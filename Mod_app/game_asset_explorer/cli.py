from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .blender_bridge import launch_blender
from .scanner import scan


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="game-assets", description="Discover and preview likely character assets")
    sub = result.add_subparsers(dest="command")
    scan_parser = sub.add_parser("scan", help="scan and print ranked character bundles")
    scan_parser.add_argument("directory", type=Path)
    scan_parser.add_argument("--json", type=Path, dest="json_path")
    first_parser = sub.add_parser("first", help="print the highest-ranked character bundle as JSON")
    first_parser.add_argument("directory", type=Path)
    view_parser = sub.add_parser("view", help="scan and open the highest-ranked supported bundle in Blender")
    view_parser.add_argument("directory", type=Path)
    view_parser.add_argument("--blender")
    view_parser.add_argument("--dry-run", action="store_true")
    frostbite_parser = sub.add_parser(
        "decode-frostbite-slice",
        help="decode a bounded CAS record using the selected game's installed Oodle runtime",
    )
    frostbite_parser.add_argument("input", type=Path)
    frostbite_parser.add_argument("output", type=Path)
    frostbite_parser.add_argument("--game-root", required=True, type=Path)
    sub.add_parser("gui", help="open the desktop interface")
    for item in (scan_parser, first_parser, view_parser):
        item.add_argument("--max-files", type=int, default=500_000)
        item.add_argument("--include-hidden", action="store_true")
    return result


def run_scan(args: argparse.Namespace):
    return scan(args.directory, max_files=args.max_files, include_hidden=args.include_hidden)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.command or args.command == "gui":
        from .gui import run_gui
        run_gui()
        return 0
    try:
        if args.command == "decode-frostbite-slice":
            from dataclasses import asdict
            from .frostbite_cas import decode_meshset_slice
            header = decode_meshset_slice(args.input, args.game_root, args.output)
            print(json.dumps(asdict(header), indent=2))
            return 0
        result = run_scan(args)
        if args.command == "scan":
            print(f"Files scanned: {result.files_seen:,}")
            print(f"Engine hints: {', '.join(result.engine_hints) or 'none'}")
            print(f"Character candidates: {len(result.bundles):,}")
            for index, bundle in enumerate(result.bundles[:20], 1):
                print(f"{index:>2}. {bundle.score:>5.1f}  {bundle.mesh.relative_path}")
            if args.json_path:
                args.json_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
                print(f"Report: {args.json_path.resolve()}")
        elif args.command == "first":
            if not result.bundles:
                print("No loose mesh candidates found.", file=sys.stderr)
                return 2
            print(json.dumps(result.bundles[0].to_dict(), indent=2))
        elif args.command == "view":
            bundle = next((b for b in result.bundles if b.mesh.directly_viewable), None)
            if not bundle:
                print("No Blender-viewable loose character mesh found; an engine adapter is required.", file=sys.stderr)
                return 2
            command = launch_blender(bundle, args.blender, args.dry_run)
            print("Launching: " + " ".join(f'\"{part}\"' if " " in part else part for part in command))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
