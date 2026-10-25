import argparse
import json
from gate import __version__

def main():
    parser = argparse.ArgumentParser(description="GATE — Gnome Automatic Test Equipment")
    parser.add_argument("--version", action="version", version=f"GATE {__version__}")
    sub = parser.add_subparsers(dest="command")
    daemon = sub.add_parser("service", help="Run acquisition and D-Bus service")
    daemon.add_argument("--demo", action="store_true")
    daemon.add_argument("--interval", type=float, default=None)
    daemon.add_argument("--control-port", type=int, default=8765, help="Loopback agent HTTP port (default 8765)")
    daemon.add_argument("--ephemeral", action="store_true", help="Do not load/save device profiles or settings")
    daemon.add_argument("--no-control-api", action="store_true")
    daemon.add_argument("--allow-raw", action="store_true", help="Allow authenticated raw instrument queries/writes")
    sub.add_parser("gui", help="Open the GTK application")
    sub.add_parser("status", help="Show service state")
    command = sub.add_parser("command", help="Queue a JSON service command")
    command.add_argument("json")
    export = sub.add_parser("export", help="Export a SQLite recording to CSV")
    export.add_argument("source")
    export.add_argument("destination")
    args = parser.parse_args()
    if args.command == "export":
        from gate.storage import export_csv
        export_csv(args.source, args.destination)
    elif args.command == "service":
        from gate.service import serve
        serve(args.demo, args.interval, None if args.no_control_api else args.control_port, args.allow_raw, args.ephemeral)
    elif args.command in ("status", "command"):
        from gate.service import proxy, call
        print(json.dumps(call(proxy(), "Snapshot" if args.command == "status" else "Command",
                              json.loads(args.json) if args.command == "command" else None), indent=2))
    else:
        from gate.app import launch
        launch()

if __name__ == "__main__":
    main()
