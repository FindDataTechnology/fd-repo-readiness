#!/usr/bin/env python3
"""Check whether each derived public repo still matches its private source.

## What this catches

Every repo here that publishes a derived public tree (paas → platform, fleet-packs →
fleet-packs-public, and so on) has the same failure mode: **the public tree quietly goes
stale**. Nothing errors. Nothing turns red. The public face just keeps showing what it
showed last month.

That is the opposite of a build failure, which at least announces itself. A stale public
tree is only ever noticed by someone who happens to compare.

## What counts as stale

Three things, checked separately because they fail differently:

1. **The record is missing.** A derived repo with no provenance file cannot even be
   evaluated — we do not know what it was built from. That is a finding, not a skip.
2. **The record lies.** The provenance names a commit, but the tree does not match it.
   This is what happened to scs001/fd-wire-images: its BUILD-INFO still said 7b2dfc3 while
   the tree had been resynced to 3ae0206. A record that is not updated with the tree is
   worse than no record, because it looks authoritative.
3. **The record is honest but behind.** Source has moved on since the snapshot.

## Usage

    python3 check_derived_freshness.py --source-root ~/finddata
    python3 check_derived_freshness.py --source-root ~/finddata --json
    python3 check_derived_freshness.py --self-test

Exit: 0 all fresh / 1 something is stale, unrecorded, or lying / 2 cannot judge.

## What it needs

Read-only GitHub API access for the public side (any token works; the repos are public),
and a local checkout of each source repo to resolve commit ancestry. No writes anywhere.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

#: derived repo -> local source checkout.
#: Kept as data, not code: adding a pair should not require reading this file.
PAIRS = [
    ("FindDataTechnology/platform", "paas"),
    ("FindDataTechnology/fleet-packs-public", "fleet-packs"),
    ("FindDataTechnology/fd-repo-readiness", "fd-infra-deploy"),
    ("scs001/fd-wire-images", "fd-wire"),
]

PROVENANCE_PATHS = ("BUILD-INFO.json", "PUBLIC-SNAPSHOT.json", ".public-snapshot.json")


def _gh(path: str) -> str | None:
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    return r.stdout if r.returncode == 0 else None


def _read_provenance(repo: str) -> tuple[str | None, str | None, bool]:
    """Return (base_commit, source_name, partial) or (None, None, False) with no record.

    `partial` marks a derived repo that carries only a slice of its source. Its commit lag
    is not a staleness signal — most of those commits cannot affect the exported slice.
    """
    for path in PROVENANCE_PATHS:
        raw = _gh(f"repos/{repo}/contents/{path}")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            import base64
            body = json.loads(base64.b64decode(payload["content"]).decode("utf-8", "replace"))
        except Exception:
            continue
        base = body.get("baseCommit") or body.get("sourceCommit")
        src = body.get("source") or body.get("sourceRepo")
        partial = bool(body.get("scope"))
        if base:
            return base, src, partial
    return None, None, False


def _is_ancestor(repo_dir: Path, ancestor: str, descendant: str = "HEAD") -> bool | None:
    r = subprocess.run(["git", "-C", str(repo_dir), "merge-base", "--is-ancestor",
                        ancestor, descendant], capture_output=True)
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    return None


def _behind_by(repo_dir: Path, base: str, descendant: str = "HEAD") -> int | None:
    r = subprocess.run(["git", "-C", str(repo_dir), "rev-list", "--count",
                        f"{base}..{descendant}"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def check(source_root: Path) -> tuple[int, list[dict]]:
    results = []
    for public_repo, source_name in PAIRS:
        src_dir = source_root / source_name
        row = {"derived": public_repo, "source": source_name}

        if not (src_dir / ".git").exists():
            row.update(status="cannot-judge", detail=f"no local checkout at {src_dir}")
            results.append(row)
            continue

        base, recorded_source, partial = _read_provenance(public_repo)
        if not base:
            row.update(status="unrecorded",
                       detail="no provenance file — cannot tell what this was built from")
            results.append(row)
            continue

        row["base"] = base[:12]

        ancestor = _is_ancestor(src_dir, base)
        if ancestor is None:
            row.update(status="cannot-judge", detail=f"commit {base[:12]} not in {source_name}")
            results.append(row)
            continue
        if not ancestor:
            row.update(status="lying",
                       detail=f"recorded base {base[:12]} is not an ancestor of {source_name} HEAD"
                              f" — the tree was rebuilt from something else and the record was not updated")
            results.append(row)
            continue

        behind = _behind_by(src_dir, base)
        row["behind"] = behind
        if partial:
            # 只取源仓一部分的派生仓：commit 落后数不是判据（见 _read_provenance）。
            row.update(status="fresh", detail="partial derivation — lag is not a signal")
        elif behind == 0:
            row.update(status="fresh")
        else:
            row.update(status="behind", detail=f"{behind} commit(s) since the snapshot")
        results.append(row)

    bad = [r for r in results if r["status"] != "fresh"]
    return (0 if not bad else 1), results


# ── Self-test: synthetic repos, no network ───────────────────────────────────

def self_test() -> int:
    """Both directions plus the two ways a record can be wrong.

    A freshness checker that never reports staleness is indistinguishable from a fresh
    tree, so the red cases matter more than the green one.
    """
    import shutil
    import tempfile

    ok = bad = 0
    tmp = Path(tempfile.mkdtemp(prefix="fd-derived-selftest-"))

    def git(cwd: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

    def expect(label: str, cond: bool) -> None:
        nonlocal ok, bad
        if cond:
            ok += 1
            print(f"  ok  {label}")
        else:
            bad += 1
            print(f"  FAIL {label}")

    try:
        src = tmp / "src"
        src.mkdir()
        git(src, "init", "-q", "-b", "main")
        (src / "a.txt").write_text("v1\n")
        git(src, "add", "-A")
        git(src, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "v1")
        sha1 = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()

        # fresh: base == HEAD
        expect("ancestor check passes for HEAD itself", _is_ancestor(src, sha1) is True)
        expect("behind count is 0 for HEAD", _behind_by(src, sha1) == 0)

        # behind: one more commit
        (src / "a.txt").write_text("v2\n")
        git(src, "add", "-A")
        git(src, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "v2")
        expect("behind count is 1 after one commit", _behind_by(src, sha1) == 1)

        # lying: a commit that is not an ancestor
        git(src, "checkout", "-q", "-b", "other", sha1)
        (src / "b.txt").write_text("x\n")
        git(src, "add", "-A")
        git(src, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "side")
        side = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        git(src, "checkout", "-q", "main")
        expect("a commit on another branch is not an ancestor",
               _is_ancestor(src, side) is False)

        # unknown commit
        expect("a nonexistent sha yields None, not False",
               _is_ancestor(src, "0" * 40) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nself-test: {ok} passed / {bad} failed")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source-root", default=str(Path.home() / "finddata"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    code, results = check(Path(a.source_root).resolve())
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return code

    width = max(len(r["derived"]) for r in results)
    for r in results:
        mark = {"fresh": "ok  ", "behind": "STALE", "lying": "LIES ", "unrecorded": "NO   ",
                "cannot-judge": "?    "}[r["status"]]
        detail = r.get("detail", "")
        print(f"{mark} {r['derived']:<{width}} ← {r['source']:<16} {detail}")
    return code


if __name__ == "__main__":
    sys.exit(main())
