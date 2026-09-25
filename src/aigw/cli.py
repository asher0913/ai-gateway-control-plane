"""``aigw simulate | serve``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .sim import compare


def _simulate(args) -> int:
    report = compare(seed=args.seed)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"{report['scenario']['requests']:,} requests over {report['scenario']['duration_s']:.0f} s\n")
    print("| Policy | Success | During outage | During brownout | p95 s | p99 s | Failed primary attempts | Cost USD |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for p in report["policies"]:
        during = p["success_rate_during"]
        cells = [
            p["policy"],
            f"{100 * p['success_rate']:.2f}%",
            f"{100 * during['outage']:.1f}%",
            f"{100 * during['brownout']:.1f}%",
            f"{p['latency_p95_s']:.2f}",
            f"{p['latency_p99_s']:.2f}",
            f"{p['failed_attempts_by_endpoint']['primary']:,}",
            f"{p['cost_usd']:.2f}",
        ]
        print("| " + " | ".join(cells) + " |")
    return 0


def _serve(args) -> int:
    import os

    import uvicorn

    from .server import API_KEYS_ENV, create_app, demo_credentials
    from .sim import reference_scenario

    if os.environ.get(API_KEYS_ENV):
        app = create_app()
    else:  # demo mode: fresh random credentials, printed once
        keys, admin = demo_credentials(reference_scenario().tenants)
        print("Demo credentials (set AIGW_API_KEYS and AIGW_ADMIN_TOKEN to use your own):")
        for key, tenant in keys.items():
            print(f"  tenant {tenant:8s} Authorization: Bearer {key}")
        print(f"  admin           Authorization: Bearer {admin}")
        app = create_app(api_keys=keys, admin_token=admin)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aigw", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sim = sub.add_parser("simulate", help="replay the fault-injection scenario under every policy")
    sim.add_argument("--seed", type=int, default=0)
    sim.add_argument("--out", help="write the full JSON report")
    sim.set_defaults(func=_simulate)
    serve = sub.add_parser("serve", help="run the OpenAI-compatible HTTP gateway with mock providers")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
