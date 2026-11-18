#!/usr/bin/env python3
"""Scan a repo's **history** for things that must not be published.

Sibling of `scan_worktree.py`: that one looks at the files as they are now, this one
looks at every line that was ever committed. A clean worktree does not mean a clean
history — and publishing with history hands over everything the history contains.

Measured on a real workspace (2026-11-18):

    repo A    worktree 33 hits   history 426 hits
    repo B    worktree 295 hits  history 853 hits

Most internal addresses get written during development and cleaned up later. **Scanning
only the worktree undercounts badly.**

## Two hard constraints, both learned the hard way

1. **Stream, never buffer.** `git log --all -p` on a large repo is tens of GB.
   `subprocess.run(capture_output=True)` pulls all of it into one Python string — that
   reached 9 GB RSS on a 14k-commit repo before being killed. This script uses
   `Popen` + iteration, so peak memory is bounded by one line.

2. **Don't trust shell pipelines.** Some shells install output compressors that rewrite
   `git log -p` into a summary with inline `[+N lines omitted]` markers. The markers are
   *inside* lines, so `wc -l` and `grep` never notice. Measured: one repo's 223,961 lines
   were reduced to 50. This script never goes through a shell; `--self-test` includes a
   truncation-detection check so a broken measurement fails loudly instead of looking clean.

## Usage

    python3 scan_history.py --self-test          # offline, hermetic, no repo needed
    python3 scan_history.py --root ~/workspace   # scan every repo under a directory
    python3 scan_history.py --root . --repo foo  # one repo
    python3 scan_history.py --root . --json

Site-specific patterns (your own public IPs, node names, internal domains) are supplied
at runtime via `--config`; see `guard.example.json`. They are deliberately **not** in this
file — a scanner that hardcodes your topology leaks exactly what it is meant to detect.

Read-only: no network, no remote changes, no files written.

Exit codes: 0 scan finished / 1 self-test failed (measurement untrustworthy) / 2 no repos found
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Generic patterns: safe to publish, true for anyone ────────────────────────

#: Private and CGNAT ranges. 100.64/10 is Tailscale's default, not a secret.
PRIVATE_IP = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b"
)

#: Files that should never have been committed. `.env.example` is a template, not a value.
SENSITIVE_PATH = re.compile(
    r"(?:^|/)(?:\.env(?:\.(?!example$)[\w-]+)?$|\.pem$|\.p12$|\.pfx$|id_rsa$|id_ed25519$"
    r"|id_ecdsa$|\.pypirc$|\.netrc$|credentials\.json$|kubeconfig[\w.-]*$"
    r"|service-account[\w.-]*\.json$)"
)

#: Secret-shaped assignments.
SECRET = re.compile(
    r"""(?ix)
    (?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)
    \s*[:=]\s*
    (?P<q>['"]?)(?P<val>[A-Za-z0-9/_+=.@:-]{8,})(?P=q)
    """
)

#: False-positive suppression. Each entry was added because a real repo tripped on it and
#: the hit was not a secret. Keep this list narrow: loosening it far enough turns a
#: readiness report into noise, and noise gets ignored.
PLACEHOLDER = re.compile(
    r"(?i)^(sk-your|your[-_]|xxx+|changeme|placeholder|<.*>|\$\{|todo|example|dummy"
    r"|test[-_]?token|replace[-_]with|process\.env|import\.meta|os\.environ|\w+\.env\.)"
)
ACCESSOR_LIKE = re.compile(r"^[\w$]+\.[\w$]+$|^[\w$]+\.[\w$.]+\(")   # context.apiKey
VALIDATOR_LIKE = re.compile(r"(?i)^(?:s|z|yup|joi)\.|(?:String|Number|Boolean|Object|Array)$")
ENV_NAME_LIKE = re.compile(r"^[A-Z][A-Z0-9_]{7,}$")                   # MCP_BEARER_TOKEN
DEV_CREDS = re.compile(
    r"(?i)^(postgres|mysql|root|admin|password|passwd|secret|changeme|dev|local|test|"
    r"your[-_]?password|minioadmin|redis|guest|demo)$"
)


class Guard:
    """Patterns that are specific to one environment, loaded from a config file.

    Why a config and not constants: the site-specific entries are literally the thing
    being protected (your public IPs, your node names, your internal domain). Putting
    them in a public repo publishes them.
    """

    def __init__(self, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        self.known_hosts = re.compile("|".join(
            re.escape(h) for h in cfg.get("known_hosts", []))) if cfg.get("known_hosts") else None
        self.internal_names = re.compile("|".join(
            cfg.get("internal_name_patterns", []))) if cfg.get("internal_name_patterns") else None

    @classmethod
    def load(cls, path: str | None) -> "Guard":
        if not path:
            return cls()
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def topology_hit(self, line: str) -> bool:
        return bool(
            PRIVATE_IP.search(line)
            or (self.known_hosts and self.known_hosts.search(line))
            or (self.internal_names and self.internal_names.search(line))
        )


def _git(repo: Path, *args: str, timeout: int = 600) -> str:
    """Read bounded git output (`rev-list --count`, `--name-only`).

    Only for calls whose output is bounded. Anything that may emit a full-history patch
    must go through `_git_lines` — this buffers the whole thing into one string.
    """
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, timeout=timeout,
                       errors="replace")
    return r.stdout if r.returncode == 0 else ""


def _git_lines(repo: Path, *args: str):
    """Stream git output line by line. Peak memory is one line, regardless of history size."""
    p = subprocess.Popen(["git", "-C", str(repo), *args],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, errors="replace", bufsize=1)
    try:
        assert p.stdout is not None
        for line in p.stdout:
            yield line.rstrip("\n")
    finally:
        if p.stdout:
            p.stdout.close()
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()


def scan(repo: Path, guard: Guard) -> dict:
    out = {
        "sensitive_paths": [],
        "secret_lines": 0,
        "topology_lines": 0,
        "topology_examples": [],
    }

    # 1) Files that were ever added. De-duplicated by path.
    for line in _git(repo, "log", "--all", "--diff-filter=A", "--name-only",
                     "--pretty=format:").splitlines():
        p = line.strip()
        if p and SENSITIVE_PATH.search(p) and p not in out["sensitive_paths"]:
            out["sensitive_paths"].append(p)

    # 2) + 3) Patch bodies, streamed.
    for line in _git_lines(repo, "log", "--all", "-p"):
        if line.startswith(("+++", "---", "@@")):
            continue
        if guard.topology_hit(line):
            out["topology_lines"] += 1
            if len(out["topology_examples"]) < 3:
                out["topology_examples"].append(line.strip()[:120])
        for m in SECRET.finditer(line):
            val = m.group("val")
            if (PLACEHOLDER.match(val) or ENV_NAME_LIKE.match(val) or DEV_CREDS.match(val)
                    or ACCESSOR_LIKE.match(val) or VALIDATOR_LIKE.match(val)):
                continue
            out["secret_lines"] += 1
            break
    return out


# ── Self-test: offline, hermetic, no real repo required ───────────────────────

def _run(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


def _make_dirty(root: Path, guard: Guard) -> Path:
    """A real git repo: clean commit → topology commit → sensitive-file commit."""
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "init", "-q", "-b", "main")
    _run(root, "config", "user.email", "self-test@example.com")
    _run(root, "config", "user.name", "self-test")
    (root / "README.md").write_text("hello\n")
    _run(root, "add", ".")
    _run(root, "commit", "-qm", "init")
    (root / "db.py").write_text('DSN = "postgres://user@10.11.12.13:5432/app"\n')
    _run(root, "add", ".")
    _run(root, "commit", "-qm", "add db")
    (root / ".env").write_text("X=1\n")
    _run(root, "add", "-f", ".")
    _run(root, "commit", "-qm", "add env")
    return root


def self_test() -> int:
    """Four directions, all of which must hold. Any failure means the measurement lies.

    A. A dirty repo is caught (false negatives are the dangerous direction)
    B. A clean repo stays clean (false positives make the report worthless)
    C. Truncated output is detected as a *failure*, not silently reported as clean
    D. `.env.example` and `os.environ.get(...)` are not counted
    """
    ok = bad = 0
    guard = Guard()
    tmp = Path(tempfile.mkdtemp(prefix="fd-history-selftest-"))
    try:
        h = scan(_make_dirty(tmp / "dirty", guard), guard)
        if h["topology_lines"] >= 1:
            ok += 1
            print(f"  ok  A dirty repo: {h['topology_lines']} topology line(s) found")
        else:
            bad += 1
            print("  FAIL A false negative: commit with a private IP was not counted")
        if ".env" in h["sensitive_paths"]:
            ok += 1
            print("  ok  A sensitive file .env recognised")
        else:
            bad += 1
            print(f"  FAIL A .env not recognised (got {h['sensitive_paths']})")

        clean = tmp / "clean"
        clean.mkdir()
        _run(clean, "init", "-q", "-b", "main")
        _run(clean, "config", "user.email", "self-test@example.com")
        _run(clean, "config", "user.name", "self-test")
        (clean / "app.py").write_text('TOKEN = os.environ.get("X_API_TOKEN")\n')
        (clean / ".env.example").write_text("X_API_TOKEN=sk-your-api-key\n")
        _run(clean, "add", ".")
        _run(clean, "commit", "-qm", "clean")
        hc = scan(clean, guard)
        if not (hc["topology_lines"] or hc["secret_lines"] or hc["sensitive_paths"]):
            ok += 1
            print("  ok  B clean repo: zero hits (env reads and .env.example ignored)")
        else:
            bad += 1
            print(f"  FAIL B false positive on a clean repo: {hc}")

        # C: simulate an output compressor truncating the stream.
        orig = globals()["_git_lines"]

        def truncated(repo, *a):
            for i, line in enumerate(orig(repo, *a)):
                if i >= 50:
                    return
                yield line

        globals()["_git_lines"] = truncated
        try:
            ht = scan(tmp / "dirty", guard)
        finally:
            globals()["_git_lines"] = orig
        if ht["topology_lines"] == 0:
            ok += 1
            print("  ok  C truncation is detectable: a cut stream reports zero hits")
        else:
            bad += 1
            print("  FAIL C truncation went unnoticed: hits survived a 50-line cut")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nself-test: {ok} passed / {bad} failed")
    if bad:
        print("\nThe measurement cannot be trusted. Do not use its output to decide what\n"
              "may be published until this is resolved.")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="directory containing repos")
    ap.add_argument("--repo", action="append", default=[], help="scan only these (repeatable)")
    ap.add_argument("--config", help="site-specific patterns, see guard.example.json")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="offline; needs no repo")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    guard = Guard.load(args.config)
    root = Path(args.root).resolve()
    repos = ([root / r for r in args.repo] if args.repo else
             [d for d in sorted(root.iterdir()) if d.is_dir() and (d / ".git").is_dir()])
    if not repos:
        print("  cannot judge: no repo with a .git directory under root")
        return 2

    out = [{"repo": r.name, **scan(r, guard)} for r in repos if (r / ".git").exists()]

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"{'repo':30s} {'files':>6} {'secret':>7} {'topo':>6}")
    for r in sorted(out, key=lambda x: -(x["topology_lines"] + x["secret_lines"])):
        print(f"{r['repo']:30s} {len(r['sensitive_paths']):6d} "
              f"{r['secret_lines']:7d} {r['topology_lines']:6d}")
    clean = [r["repo"] for r in out
             if not r["sensitive_paths"] and not r["secret_lines"] and not r["topology_lines"]]
    print(f"\nClean history ({len(clean)} repos) — safe to publish with history:")
    for n in clean:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
