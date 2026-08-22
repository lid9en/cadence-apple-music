"""Standalone CLI for the Apple Music instant-skip diagnosis.

Thin wrapper over cadence.applemusic_fix so the logic lives in exactly one
place. Run it without the GUI:

    python tools/diagnose_skip.py --seconds 30
    python tools/diagnose_skip.py --logs
    python tools/diagnose_skip.py --scan
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cadence import applemusic_fix as fix          # noqa: E402
from cadence.config import Settings                # noqa: E402
from cadence.media import MediaEngine              # noqa: E402


def cmd_scan() -> None:
    r = fix.scan()
    print(f"Apple Music {r['version'] or '?'} — "
          f"{'running' if r['running'] else 'not running'}\n")
    for f in r["findings"]:
        print(f"[{f['severity'].upper():5}] {f['title']}")
        print(f"        {f['detail']}")
        if f.get("note"):
            print(f"        note: {f['note']}")
        print()


def cmd_logs() -> None:
    r = fix.analyze_logs()
    if not r.get("available"):
        print("no logs:", r.get("reason"))
        return
    print("scanned:", ", ".join(r["scanned"]))
    print("verdict:", r["verdict"])
    print(r["summary"], "\n")
    for f in r["findings"]:
        print(f"{f['count']:6d}x  {f['label']}")
        print(f"         {f['explanation']}")
        if f["sample"]:
            print(f"         sample: {f['sample']}")
        print()


def cmd_watch(seconds: int, follow_any: bool) -> None:
    s = Settings()
    s.update({"source": {"mode": "any" if follow_any else "apple",
                         "poll_ms": 100}}, save=False)
    engine = MediaEngine(s)
    engine.start()

    print("=" * 68)
    print(f"Watching for {seconds}s. START PLAYBACK IN APPLE MUSIC NOW.")
    print("Only media/volume keys are recorded; ordinary typing is ignored.")
    print("=" * 68, flush=True)

    try:
        r = fix.watch(seconds, engine)
    finally:
        engine.stop()

    print(f"\nverdict            : {r['verdict']}")
    print(f"track changes      : {r['track_changes']}")
    print(f"NEXT key events    : {r['next_key_events']} "
          f"({r['injected_keys']} software-injected)")
    print(f"changes after key  : {r['changes_preceded_by_key']}")
    print(f"median gap         : {r['median_gap_seconds']}s")
    print(f"\n{r['advice']}")
    if r["verdict"] == "internal":
        print("\nNext: python tools/diagnose_skip.py --logs")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan", action="store_true", help="read-only system scan")
    ap.add_argument("--logs", action="store_true", help="decode Apple's ETL traces")
    ap.add_argument("--seconds", type=int, default=30, help="live watch duration")
    ap.add_argument("--any", action="store_true",
                    help="follow any player, not just Apple Music")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    args = ap.parse_args()

    if args.json:
        out = {"scan": fix.scan()} if args.scan else {"logs": fix.analyze_logs()}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if args.scan:
        cmd_scan()
    elif args.logs:
        cmd_logs()
    else:
        cmd_watch(args.seconds, args.any)
    return 0


if __name__ == "__main__":
    sys.exit(main())
