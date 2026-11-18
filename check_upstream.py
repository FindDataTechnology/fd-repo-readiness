#!/usr/bin/env python3
"""Detect drift between these adapted copies and the upstream files they came from.

## What this catches

Every file in `openspec-guards/` is an **adaptation**, not a copy. Upstream is a private
workspace where these guards run against its own OpenSpec trees; here they need a different
default root, no internal history, and broader waiver markers. Those differences are
deliberate and permanent, so the files can never match byte for byte — which is exactly why
drift is easy to miss.

Two independent failure modes, needing two separate checks:

1. **Upstream moved.** The file in the source repo changed. Either the fix should come over
   here too, or it is upstream-specific and this adaptation should say so. Silence is a bug
   either way: this repo keeps passing its own tests while checking logic that upstream
   already replaced.
2. **Local edit.** Somebody edited a file here without going through adaptation, so the next
   regeneration silently reverts it. `adapted_sha256` catches that even when upstream has
   not moved.

Both are recorded in `upstream.json`, which is generated from the source repo's git blob
SHAs and the local file hashes.

## Why blob SHAs and not content diffs

A three-way diff would need the adapted file, the old upstream file, and the new one — the
old upstream blob is only available if the source repo happens to still have it after a
rebase or force push. Blob SHAs are cheap, stable, and answer the question actually being
asked ("has this changed?"). When they do differ, a human runs the diff.

## Usage

    python3 check_upstream.py                      # report
    python3 check_upstream.py --source ../fd-infra-deploy
    python3 check_upstream.py --self-test          # offline, synthetic repos
    python3 check_upstream.py --write-manifest     # re-stamp after a deliberate refresh

Exit: 0 in sync / 1 drift / 2 cannot judge (source repo or manifest unavailable).
Nothing here talks to the network: the source repo must be checked out locally.

## Not covered

This runs in CI as a **scheduled** job, not on every push — CI has no access to the private
source repo. A push-time run would report "cannot judge" forever and train people to ignore
it. The scheduled run happens on a machine that has the source checked out.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

GUARDS_DIR = "openspec-guards"
MANIFEST = "upstream.json"


def _blob_sha(repo: Path, rel: str) -> str | None:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", f"HEAD:{rel}"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def check(root: Path, source: Path) -> tuple[int, list[str]]:
    """Return (exit_code, lines). Exit 2 (cannot judge) is returned as code 2."""
    lines: list[str] = []
    mpath = root / GUARDS_DIR / MANIFEST
    if not mpath.exists():
        return 2, [f"cannot judge: {mpath} not found"]
    if not (source / ".git").exists():
        return 2, [f"cannot judge: {source} is not a git checkout"]

    man = json.loads(mpath.read_text(encoding="utf-8"))
    src_rel = man.get("source_path", "tools/")
    drift = 0

    for name, rec in sorted(man.get("files", {}).items()):
        local = root / GUARDS_DIR / name
        if not local.exists():
            lines.append(f"DRIFT {name}: listed in the manifest but missing here")
            drift += 1
            continue

        want_local = rec.get("adapted_sha256")
        got_local = _sha256(local)
        if want_local and got_local != want_local:
            lines.append(f"DRIFT {name}: edited locally without re-stamping the manifest "
                         f"({want_local[:12]}… → {got_local[:12]}…)")
            drift += 1

        want_up = rec.get("blob_sha")
        got_up = _blob_sha(source, f"{src_rel}{name}")
        if got_up is None:
            lines.append(f"DRIFT {name}: gone from upstream {src_rel}")
            drift += 1
        elif want_up and got_up != want_up:
            lines.append(f"DRIFT {name}: upstream moved ({want_up[:12]}… → {got_up[:12]}…) — "
                         f"decide whether the change belongs here, then re-stamp")
            drift += 1

    if not drift:
        lines.append(f"in sync: {len(man.get('files', {}))} adapted file(s), "
                     f"source {man.get('source_repo')}@{man.get('source_commit', '')[:12]}")
    return (1 if drift else 0), lines


def write_manifest(root: Path, source: Path) -> int:
    mpath = root / GUARDS_DIR / MANIFEST
    if not mpath.exists():
        print(f"no manifest at {mpath} to re-stamp", file=sys.stderr)
        return 2
    man = json.loads(mpath.read_text(encoding="utf-8"))
    src_rel = man.get("source_path", "tools/")
    man["source_commit"] = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    for name, rec in man.get("files", {}).items():
        rec["blob_sha"] = _blob_sha(source, f"{src_rel}{name}") or rec.get("blob_sha")
        local = root / GUARDS_DIR / name
        if local.exists():
            rec["adapted_sha256"] = _sha256(local)
    mpath.write_text(json.dumps(man, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"re-stamped {mpath}")
    return 0


# ── Self-test: synthetic repos, no network, no real source ────────────────────

def self_test() -> int:
    """Both drift directions must go red, and the clean case must go green.

    A checker for drift that never reports drift is indistinguishable from a synced repo,
    so the red cases matter more than the green one.
    """
    ok = bad = 0
    tmp = Path(tempfile.mkdtemp(prefix="fd-upstream-selftest-"))
    try:
        src = tmp / "src"
        (src / "tools").mkdir(parents=True)
        subprocess.run(["git", "-C", str(src), "init", "-q", "-b", "main"], check=True)
        (src / "tools" / "guard.py").write_text("# upstream v1\n")
        subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(src), "-c", "user.email=t@e", "-c", "user.name=t",
                        "commit", "-qm", "v1"], check=True)

        repo = tmp / "repo"
        (repo / GUARDS_DIR).mkdir(parents=True)
        (repo / GUARDS_DIR / "guard.py").write_text("# adapted v1\n")

        import json as _json
        _json.dump({"source_repo": "src", "source_path": "tools/", "source_commit": "",
                    "files": {"guard.py": {
                        "blob_sha": _blob_sha(src, "tools/guard.py"),
                        "adapted_sha256": _sha256(repo / GUARDS_DIR / "guard.py")}}},
                   (repo / GUARDS_DIR / MANIFEST).open("w"))

        code, lines = check(repo, src)
        if code == 0:
            ok += 1
            print("  ok  in-sync repo reports 0")
        else:
            bad += 1
            print(f"  FAIL clean repo reported drift: {lines}")

        (src / "tools" / "guard.py").write_text("# upstream v2\n")
        subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(src), "-c", "user.email=t@e", "-c", "user.name=t",
                        "commit", "-qm", "v2"], check=True)
        code, lines = check(repo, src)
        if code == 1 and any("upstream moved" in l for l in lines):
            ok += 1
            print("  ok  upstream change is caught")
        else:
            bad += 1
            print(f"  FAIL upstream change not caught: {code} {lines}")

        write_manifest(repo, src)
        code, lines = check(repo, src)
        if code == 0:
            ok += 1
            print("  ok  re-stamp clears the upstream drift")
        else:
            bad += 1
            print(f"  FAIL re-stamp did not clear drift: {lines}")

        (repo / GUARDS_DIR / "guard.py").write_text("# adapted v2, hand-edited\n")
        code, lines = check(repo, src)
        if code == 1 and any("edited locally" in l for l in lines):
            ok += 1
            print("  ok  local hand-edit is caught")
        else:
            bad += 1
            print(f"  FAIL local edit not caught: {code} {lines}")

        code, lines = check(repo, tmp / "nope")
        if code == 2:
            ok += 1
            print("  ok  missing source reports 'cannot judge' (2), not clean")
        else:
            bad += 1
            print(f"  FAIL missing source returned {code}, expected 2")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nself-test: {ok} passed / {bad} failed")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--source", default="../fd-infra-deploy",
                    help="local checkout of the source repo (default: ../fd-infra-deploy)")
    ap.add_argument("--write-manifest", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    root = Path(a.root).resolve()
    if a.write_manifest:
        return write_manifest(root, Path(a.source).resolve())

    code, lines = check(root, Path(a.source).resolve())
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
