import sys, time, subprocess
from pathlib import Path
from tests.client import Client
S = Path(sys.argv[1])
pairs = [p.split(':') for p in sys.argv[2:]]
c = Client()
ok = bad = 0
for src, to in pairs:
    data = (S / f'sample.{src}').read_bytes()
    t = time.monotonic()
    view, out = c.convert(data, to, f'sample.{src}', src, timeout=200)
    route = ' > '.join(s['engine'] for s in (view.get('route') or {}).get('steps', []))
    if view['state'] == 'done':
        ok += 1
        print(f"  ok    {src:>8} -> {to:<8} {len(out):>9} B {time.monotonic()-t:5.1f}s  {route}", flush=True)
    else:
        bad += 1
        print(f"  FAIL  {src:>8} -> {to:<8} {time.monotonic()-t:5.1f}s  {route}  {str(view.get('error'))[:60]}", flush=True)
print(f'{ok} ok, {bad} failed')
