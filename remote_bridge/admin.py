"""Device administration CLI (run on the gateway host, as the gateway's user).

    python -m remote_bridge.admin add pc-casa --label "PC de casa"
    python -m remote_bridge.admin list
    python -m remote_bridge.admin revoke pc-casa
    python -m remote_bridge.admin rotate pc-casa      # new token, old one stops working
    python -m remote_bridge.admin policy pc-casa      # show the server-side policy
    python -m remote_bridge.admin policy pc-casa --set desktop.screenshot=allow

`add` and `rotate` print the token ONCE (to stdout, nowhere else). Copy it to the
PC with `windows\\install_connector.ps1 -SetToken`, which stores it with DPAPI.
A running gateway picks every change up within seconds, no restart needed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .policy import DEFAULT_POLICY, LEVELS
from .registry import DeviceRegistry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="remote_bridge.admin")
    ap.add_argument("--data-dir", default=os.environ.get("BRIDGE_DATA_DIR", "/var/lib/alejandro-gateway"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("device_id")
    p_add.add_argument("--label", default="")
    sub.add_parser("list")
    sub.add_parser("revoke").add_argument("device_id")
    sub.add_parser("rotate").add_argument("device_id")
    p_pol = sub.add_parser("policy")
    p_pol.add_argument("device_id")
    p_pol.add_argument("--set", action="append", default=[], metavar="TOOL=LEVEL")
    args = ap.parse_args(argv)

    reg = DeviceRegistry(Path(args.data_dir) / "devices.json")

    if args.cmd == "add":
        if reg.get(args.device_id) and not reg.get(args.device_id).get("revoked_at"):
            print(f"{args.device_id} already exists; use 'rotate' to issue a new token", file=sys.stderr)
            return 1
        token = reg.add(args.device_id, label=args.label, tools=dict(DEFAULT_POLICY))
        print(token)
        print(f"# device {args.device_id} enrolled. The token above is shown only once.", file=sys.stderr)
        return 0
    if args.cmd == "list":
        print(json.dumps(reg.list(), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "revoke":
        if not reg.revoke(args.device_id):
            print("unknown device", file=sys.stderr)
            return 1
        print(f"{args.device_id} revoked", file=sys.stderr)
        return 0
    if args.cmd == "rotate":
        entry = reg.get(args.device_id)
        if not entry:
            print("unknown device", file=sys.stderr)
            return 1
        print(reg.add(args.device_id, label=entry.get("label", ""), tools=entry.get("tools")))
        print("# new token issued; the old one no longer works.", file=sys.stderr)
        return 0
    if args.cmd == "policy":
        entry = reg.get(args.device_id)
        if not entry:
            print("unknown device", file=sys.stderr)
            return 1
        tools = dict(entry.get("tools") or {})
        for item in args.set:
            tool, _, level = item.partition("=")
            if level not in LEVELS:
                print(f"bad level {level!r}; use one of {LEVELS}", file=sys.stderr)
                return 2
            tools[tool] = level
        if args.set:
            reg.set_tools(args.device_id, tools)
        print(json.dumps(tools, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
