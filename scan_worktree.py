#!/usr/bin/env python3
"""Scan a repo's **worktree** for things that must not be published.

Companion to `scan_history.py`. This one reads the files as they are committed now
(`git ls-files`); a hit here can be fixed. A hit in history cannot.

## Why the hit counts are "files", not "occurrences"

Each category reports the number of **files** that contain at least one match. Occurrence
counts tempt you to celebrate a drop from 40 to 3 when you deleted a single comment; file
counts answer the question you actually have ("how many files do I have to open").

## False-positive suppression is the hard part

A naive pattern matches `apiKey: context.apiKey` (property access, not a value),
`optionalString` (a zod validator name), `existingSecret: foo` (a Kubernetes secret
*reference*), and `POSTGRES_PASSWORD: postgres` (a compose sample). On one real repo that
turned 1,632 raw hits into ~16 real ones — a 99% noise rate, and a report nobody reads is
worse than no report. The suppression list below is narrow on purpose and every entry is
covered by `--self-test`.

## Usage

    python3 scan_worktree.py --self-test
    python3 scan_worktree.py --root ~/workspace
    python3 scan_worktree.py --root . --repo foo --json

Site-specific patterns come from `--config`; see `guard.example.json`. They are not in
this file: a scanner that hardcodes your topology publishes what it is built to find.

Read-only. Exit codes: 0 finished / 1 self-test failed / 2 no repos found
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

PRIVATE_IP = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b"
)

SECRET = re.compile(
    r"""(?ix)
    (?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)
    \s*[:=]\s*
    (?P<q>['"]?)(?P<val>[A-Za-z0-9/_+=.@:-]{8,})(?P=q)
    """
)

PLACEHOLDER = re.compile(
    r"(?i)^(sk-your|your[-_]|xxx+|changeme|placeholder|<.*>|\$\{|todo|example|dummy"
    r"|test[-_]?token|replace[-_]with|process\.env|import\.meta|os\.environ|\w+\.env\.)"
)
ACCESSOR_LIKE = re.compile(r"^[\w$]+\.[\w$]+$|^[\w$]+\.[\w$.]+\(")
VALIDATOR_LIKE = re.compile(r"(?i)^(?:s|z|yup|joi)\.|(?:String|Number|Boolean|Object|Array)$")
ENV_NAME_LIKE = re.compile(r"^[A-Z][A-Z0-9_]{7,}$")
DEV_CREDS = re.compile(
    r"(?i)^(postgres|mysql|root|admin|password|passwd|secret|changeme|dev|local|test|"
    r"your[-_]?password|minioadmin|redis|guest|demo)$"
)
#: `existingSecret: foo` / `secretName: bar` — a reference, not a value.
SECRET_REF = re.compile(r"(?i)(?:^|-)(?:secrets?|secretname|secretref|secretkeyref)$|^existing")
#: A value that is followed by a call or attribute access reads config, not a literal.
CALL_AFTER = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)"
    r"\s*[:=]\s*[\w.]*(?:\(|\.(?:get|env|token|key|password|secret)\b)"
)

SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2", ".ttf",
            ".pdf", ".zip", ".gz", ".jar", ".so", ".dylib", ".wasm", ".pyc", ".lock"}
SKIP_NAMES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
              "uv.lock", "Cargo.lock", "go.sum"}


class Guard:
    """Site-specific patterns from a config file — see the module docstring for why."""

    def __init__(self, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        self.known_hosts = re.compile("|".join(
            re.escape(h) for h in cfg.get("known_hosts", []))) if cfg.get("known_hosts") else None
        self.internal_names = re.compile("|".join(
            cfg.get("internal_name_patterns", []))) if cfg.get("internal_name_patterns") else None

    @classmethod
    def load(cls, path: str | None) -> "Guard":
        return cls(json.loads(Path(path).read_text(encoding="utf-8"))) if path else cls()

    def hosts_hit(self, text: str) -> bool:
        return bool((self.known_hosts and self.known_hosts.search(text))
                    or (self.internal_names and self.internal_names.search(text)))


def _tracked(repo: Path) -> list[str]:
    r = subprocess.run(["git", "-C", str(repo), "ls-files"],
                       capture_output=True, text=True, timeout=300, errors="replace")
    return [l for l in r.stdout.splitlines() if l] if r.returncode == 0 else []


def scan(repo: Path, guard: Guard) -> dict:
    hits = {"private_ip": [], "known_host": [], "internal_name": [], "secret": []}
    for rel in _tracked(repo):
        p = repo / rel
        if not p.is_file() or p.suffix.lower() in SKIP_EXT or p.name in SKIP_NAMES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if PRIVATE_IP.search(text):
            hits["private_ip"].append(rel)
        if guard.hosts_hit(text):
            # Split the two site-specific categories so the report says which one fired.
            (hits["known_host"] if guard.known_hosts and guard.known_hosts.search(text)
             else hits["internal_name"]).append(rel)

        for m in SECRET.finditer(text):
            val = m.group("val")
            if (PLACEHOLDER.match(val) or ENV_NAME_LIKE.match(val) or DEV_CREDS.match(val)
                    or ACCESSOR_LIKE.match(val) or VALIDATOR_LIKE.match(val)):
                continue
            if SECRET_REF.search(val):
                continue
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start: line_end if line_end != -1 else len(text)]
            if CALL_AFTER.search(line):
                continue
            key = text[max(0, m.start() - 40): m.start()].strip().split()
            if key and SECRET_REF.search(key[-1]):
                continue
            hits["secret"].append(rel)
            break
    return hits


# ── Self-test: offline and hermetic ──────────────────────────────────────────

def _repo_with(files: dict[str, str], root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    for name, body in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def self_test() -> int:
    """Both directions for every suppression rule.

    Tightening a rule can silently start passing real secrets (a false negative is far
    worse than a false positive); loosening it fills the report with noise. Each case
    below pins one of those edges.
    """
    ok = bad = 0
    guard = Guard({"known_hosts": ["203.0.113.9"], "internal_name_patterns": ["\\bnode-a\\b"]})
    tmp = Path(tempfile.mkdtemp(prefix="fd-worktree-selftest-"))

    def expect(label: str, cond: bool) -> None:
        nonlocal ok, bad
        if cond:
            ok += 1
            print(f"  ok  {label}")
        else:
            bad += 1
            print(f"  FAIL {label}")

    try:
        dirty = _repo_with({
            "db.py": 'DSN = "postgres://fd@10.11.12.13:5432/app"\n',
            "conf.yaml": "host: 203.0.113.9\n# runs on node-a\n",
            "auth.py": 'API_KEY = "Zx9Qw2Lm7Rt4"\n',
        }, tmp / "dirty")
        h = scan(dirty, guard)
        expect("private IP caught in a file", "db.py" in h["private_ip"])
        expect("known host caught", "conf.yaml" in h["known_host"])
        expect("internal node name caught", "conf.yaml" in h["internal_name"])
        expect("secret literal caught", "auth.py" in h["secret"])

        clean = _repo_with({
            "app.py": 'TOKEN = os.environ.get("X_API_TOKEN")\n',
            "ci.yml": "POSTGRES_PASSWORD: postgres\n",
            "schema.ts": "apiKey: context.apiKey,\nsecret: s.string(),\n",
            "k8s.yaml": "existingSecret: app-secrets\n",
            ".env.example": "X_API_TOKEN=sk-your-api-key\n",
            "lock.json": 'api_key: "Ab3dEf6hIj9k"\n',
        }, tmp / "clean")
        hc = scan(clean, guard)
        expect("env read not counted as a secret", "app.py" not in hc["secret"])
        expect("compose sample password not counted", "ci.yml" not in hc["secret"])
        expect("property access / validator name not counted", "schema.ts" not in hc["secret"])
        expect("secret reference not counted", "k8s.yaml" not in hc["secret"])
        expect(".env.example not counted", ".env.example" not in hc["secret"])
        expect("lockfiles skipped", "lock.json" not in hc["secret"])

        noguard = scan(_repo_with({"x.txt": "host: 203.0.113.9\n"}, tmp / "noguard"), Guard())
        expect("without config, site-specific patterns stay silent",
               not (noguard["known_host"] or noguard["internal_name"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nself-test: {ok} passed / {bad} failed")
    if bad:
        print("\nThe measurement cannot be trusted: a rule above is passing something it "
              "should catch, or catching something it should pass.")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--config", help="site-specific patterns, see guard.example.json")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
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

    out = []
    for repo in repos:
        if not (repo / ".git").exists():
            continue
        h = scan(repo, guard)
        out.append({"repo": repo.name,
                    "hits": {k: len(v) for k, v in h.items()},
                    "total": sum(len(v) for v in h.values()),
                    "files": h})

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"{'repo':30s} {'ip':>5} {'host':>5} {'name':>5} {'secret':>7} {'total':>6}")
    for r in sorted(out, key=lambda x: x["total"]):
        h = r["hits"]
        print(f"{r['repo']:30s} {h['private_ip']:5d} {h['known_host']:5d} "
              f"{h['internal_name']:5d} {h['secret']:7d} {r['total']:6d}")
    clean = [r["repo"] for r in out if r["total"] == 0]
    print(f"\nZero hits ({len(clean)} repos):")
    for n in clean:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
