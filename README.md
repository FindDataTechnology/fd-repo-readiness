# fd-repo-readiness

Two read-only scanners that answer one question before a private repository is published:

> **What is in this repo that should not leave the building?**

```
scan_worktree.py   the files as they are committed now  — a hit here can be FIXED
scan_history.py    every line that was ever committed   — a hit here CANNOT be fixed
```

They report separately because the two answers require different actions. A clean worktree
with a dirty history is the common case, and reporting one combined number hides it.

## Why both

Measured on a real workspace (2026-11-18):

| repo | worktree hits | history hits |
|---|---:|---:|
| A | 33 | 426 |
| B | 295 | 853 |

Most internal addresses get typed during development and cleaned up later. **Scanning only
the worktree undercounts by an order of magnitude** — and the missing part is the part you
can no longer fix.

## Usage

```bash
# Always start here. Offline, hermetic, no repo required.
python3 scan_worktree.py --self-test
python3 scan_history.py  --self-test

# Copy the template, fill in your own hosts and node names.
cp guard.example.json guard.json && $EDITOR guard.json

python3 scan_worktree.py --root ~/workspace --config guard.json
python3 scan_history.py  --root ~/workspace --config guard.json --json > history.json
```

Exit codes: `0` finished · `1` self-test failed (the measurement cannot be trusted) ·
`2` nothing to scan (this is *not* a pass).

## What each finds

| category | worktree | history | meaning |
|---|:-:|:-:|---|
| private / CGNAT IP | yes | yes | RFC1918 and `100.64/10`; a hit means internal addressing |
| known host | yes | yes | your own public IPs, from `--config` |
| internal hostname | yes | yes | node names, internal domains, from `--config` |
| secret literal | yes | yes | a password/token/key **value**, not a variable name |
| sensitive file | — | yes | `.env`, `*.pem`, `id_rsa`, `kubeconfig`, `credentials.json` |

History also flags files that were added and later deleted — they still exist in the objects
and are still readable by SHA after a `force push`. Moving a ref does not delete history.

## Site-specific patterns live in a config file, not in this repo

The hostnames and IPs being searched for are often the exact thing that must not be
published. Hardcoding them here would leak what the tool exists to find. `guard.example.json`
contains invented values; copy it, replace them, and keep `guard.json` out of git.

## Design notes

**Peak memory is one line, not one history.** `git log --all -p` on a 14k-commit repo is
tens of GB. Reading it with `subprocess.run(capture_output=True)` reached 9 GB RSS before
being killed, so both scripts use `Popen` and iterate. Same reason they never pass through a
shell: output compressors rewrite large `git log -p` streams into summaries with inline
`[+N lines omitted]` markers, and the markers are *inside* lines — `wc -l` and `grep` never
notice. One 223,961-line stream arrived as 50 lines and looked fine.

**False-positive suppression is load-bearing.** A naive secret pattern matched 1,632 things
in one repo of which roughly 16 were real: `apiKey: context.apiKey` is a property access,
`optionalString` is a validator name, `existingSecret: foo` is a reference,
`POSTGRES_PASSWORD: postgres` is a compose sample. A report that is 99% noise gets ignored,
which is the same as not scanning. Every suppression rule has a case in `--self-test`,
in both directions — tightening a rule can start silently passing a real secret, and that
direction is much worse.

**Truncation is tested, not assumed.** `scan_history.py --self-test` feeds itself a
deliberately truncated stream and requires the result to be detected as a failure. If that
check ever stops failing, the tool reports a clean history that it never actually read.

## Not covered

- **Non-UTF-8 and binary content.** Files are read with `errors="ignore"`.
- **Entropy-based secret detection.** Patterns only; a high-entropy string with no keyword
  nearby is missed. Pair with `gitleaks` for that.
- **Third-party licence compatibility.** Whether bundled corpora or caches may be
  redistributed is a judgement call, not a scan.
- **Whether publishing is a good idea.** These report what is present. Deciding what that
  means for your organisation is not a script.

## Licence

MIT. See `LICENSE`.
