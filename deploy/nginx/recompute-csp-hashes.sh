#!/usr/bin/env bash
#
# Recompute the CSP script-src sha256 pins from the pages in site/ and write
# them into all three configs that carry them.
#
# WHY THIS EXISTS
#
#   deploy/Caddyfile, deploy/nginx/security-headers.conf and PART 2j of
#   deploy/nginx/yangble5.com.conf.example each pin the inline <script> of
#   site/index.html and site/verify.html by sha256, so that a page whose job is
#   convincing a visitor it is safe to paste a command into a shell cannot be
#   made to run injected script.
#
#   A hash pin has a nasty failure mode: edit a page, forget a config, and the
#   page still RENDERS. Only the copy buttons, the OS detection and the live
#   pool status stop working, silently, with the explanation visible nowhere
#   except a browser console nobody opens. Three copies of the pin makes that
#   three chances to forget.
#
#   tests/test_deploy_security_headers.py turns that into a red test. This
#   script is how you make it green again without editing three files by hand.
#
# USAGE
#
#   bash deploy/nginx/recompute-csp-hashes.sh          # rewrite the configs
#   bash deploy/nginx/recompute-csp-hashes.sh --check  # report only, exit 1 if stale
#
#   Run it after ANY edit to an inline <script> in site/*.html — same pass in
#   which you regenerate site/*.sha256, for the same reason.
#
# It only ever replaces sha256- tokens inside a script-src. It does not add,
# remove or reorder any directive, and `--check` writes nothing.
#
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

command -v python3 >/dev/null 2>&1 && PY=python3 || PY=python

"$PY" - "$ROOT" "$CHECK" <<'PYEOF'
import base64, hashlib, pathlib, re, sys

root = pathlib.Path(sys.argv[1])
check_only = sys.argv[2] == "1"

# The pages, in the order the CSP lists them. index.html first, verify.html
# second: the order is not functionally significant to a browser, but keeping
# it stable keeps the diff readable and the comments in the configs true.
PAGES = ["index.html", "verify.html"]
TARGETS = [
    "deploy/Caddyfile",
    "deploy/nginx/security-headers.conf",
    "deploy/nginx/yangble5.com.conf.example",
    # site/README.md carries the pin TWICE: once inside a sample add_header
    # (handled by the script-src rewrite below) and once as a per-page table
    # (handled by PAGE_TABLE). It was left out of this list originally, so the
    # first edit after the script was written left it stale and sitecheck went
    # red -- the exact "three chances to forget" failure the header comment
    # describes, except there are four copies, not three.
    "site/README.md",
]

# `index.html    'sha256-…'` in the documentation table. The script-src regex
# cannot see this form because there is no script-src directive on the line.
PAGE_TABLE = re.compile(
    r"^(?P<pad>(?P<name>index\.html|verify\.html)\s+)'sha256-[^']*'$", re.M
)

INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)

hashes = []
per_page = {}
for name in PAGES:
    page = root / "site" / name
    if not page.exists():
        sys.exit(f"missing {page}")
    found = INLINE.findall(page.read_text(encoding="utf-8"))
    if not found:
        sys.exit(f"{name}: no inline <script> found — the CSP comments say there is one")
    for body in found:
        digest = hashlib.sha256(body.encode("utf-8")).digest()
        hashes.append("sha256-" + base64.b64encode(digest).decode())
    per_page[name] = hashes[-1]
    print(f"  {name}: {len(found)} inline script(s)")

want = " ".join(f"'{h}'" for h in hashes)
print(f"  script-src pins: {want}")

stale = 0
for rel in TARGETS:
    path = root / rel
    text = path.read_text(encoding="utf-8")

    def fix(m):
        # Replace every quoted sha256- token inside this script-src with the
        # freshly computed list, preserving everything else in the directive.
        directive = m.group(0)
        first = re.search(r"'sha256-[^']*'", directive)
        if not first:
            return directive
        directive = re.sub(r"'sha256-[^']*'\s*", "", directive)
        return directive[:first.start()] + want + " " + directive[first.start():]

    new = re.sub(r"script-src[^;\"]*", fix, text)
    new = PAGE_TABLE.sub(lambda m: m["pad"] + f"'{per_page[m['name']]}'", new)
    if new == text:
        print(f"  ok    {rel}")
        continue
    stale += 1
    if check_only:
        print(f"  STALE {rel}")
    else:
        path.write_text(new, encoding="utf-8", newline="")
        print(f"  wrote {rel}")

if check_only and stale:
    sys.exit(f"\n{stale} config(s) carry stale CSP hashes. Re-run without --check.")
PYEOF

if [ "$CHECK" -eq 1 ]; then
    echo "CSP hashes are current."
else
    echo
    echo "Done. Now run:  python -m pytest tests/test_deploy_security_headers.py -q"
fi
