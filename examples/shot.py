#!/usr/bin/env python3
"""Fast single screenshot from the running executor via the Bridge.

One round trip, no sleeps, optional crop — built for catching transient UI
(success toasts, a transient confirmation checkmark) that a slow multi-step capture
misses. Assumes the Bridge is reachable at localhost:7878.

Usage:
    examples/shot.py                         -> /tmp/shot.png (full frame)
    examples/shot.py out.png                 -> save to out.png
    examples/shot.py out.png 560,210,1120,640  -> save cropped to that box
    examples/shot.py --burst 5               -> 5 rapid frames /tmp/shot_0..4.png
"""
import base64, json, os, sys, urllib.request

BRIDGE = "http://localhost:7878/call"
BRIDGE_TOKEN = os.environ.get("REALHANDS_BRIDGE_TOKEN")


def grab(path):
    headers = {"Content-Type": "application/json"}
    if BRIDGE_TOKEN:
        headers["X-RealHands-Token"] = BRIDGE_TOKEN
    req = urllib.request.Request(
        BRIDGE,
        data=json.dumps({"method": "screenshot", "params": {}}).encode(),
        headers=headers,
    )
    d = json.loads(urllib.request.urlopen(req, timeout=15).read())
    r = d.get("result")
    if not r:
        print("ERROR:", d, file=sys.stderr)
        sys.exit(1)
    raw = base64.b64decode(r["base64"])
    open(path, "wb").write(raw)
    return r, len(raw)


def main():
    args = [a for a in sys.argv[1:]]
    if args and args[0] == "--burst":
        n = int(args[1]) if len(args) > 1 else 5
        for i in range(n):
            r, _ = grab(f"/tmp/shot_{i}.png")
        print(f"burst {n} -> /tmp/shot_0..{n-1}.png  url={r.get('url','')[:60]}")
        return
    out = args[0] if args else "/tmp/shot.png"
    crop = args[1] if len(args) > 1 else None
    r, nbytes = grab(out)
    if crop:
        from PIL import Image
        box = tuple(int(v) for v in crop.split(","))
        Image.open(out).crop(box).save(out)
    print(f"{out}  dpr={r.get('device_pixel_ratio')}  url={r.get('url','')[:60]}")


if __name__ == "__main__":
    main()
