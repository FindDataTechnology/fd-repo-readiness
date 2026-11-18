# fd-repo-readiness

Tools that answer questions about a repository nobody wants to answer from memory.

```
scan_worktree.py   the files as they are committed now  — a hit here can be FIXED
scan_history.py    every line that was ever committed   — a hit here CANNOT be fixed
```

The scanners answer one question before a private repository is published:

> **What is in this repo that should not leave the building?**

They report separately because the two answers require different actions. A clean worktree
with a dirty history is the common case, and reporting one combined number hides it.

`openspec-guards/` holds three checks for [OpenSpec](https://github.com/openspec) hygiene —
archived/live change duplicates, unexplained unticked tasks, and capabilities that only
exist inside an archived delta. Each is a defect that validates green and is invisible to
every other check. See [Adapted files and upstream drift](#adapted-files-and-upstream-drift).


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

## Adapted files and upstream drift

`openspec-guards/` holds three guards **adapted** from a private workspace, not copied. The
differences are permanent and deliberate: `--root` defaults to the cwd rather than to the
source tree, internal history is removed, and waiver markers accept English as well as
Chinese. So the files can never match upstream byte for byte, and drift is easy to miss.

`upstream.json` records, per file, the upstream git blob SHA it was adapted from and the
sha256 of the adaptation here. `check_upstream.py` reads both:

```
upstream moved  →  decide whether the change belongs here, then --write-manifest
edited locally  →  someone bypassed adaptation; regeneration would silently revert it
```

```
python3 check_upstream.py --self-test            # offline, synthetic repos
python3 check_upstream.py --source ../fd-infra-deploy
python3 check_upstream.py --source ../fd-infra-deploy --write-manifest
```

Two drift directions need two checks. The blob SHA answers "has upstream changed?"; the
local hash answers "has this file changed here without re-stamping?". Checking only the
first means a hand-edit survives until the next regeneration, which reverts it with no
warning.

**The drift check does not run in CI, deliberately.** It needs a local checkout of the
private source repo, which this repo has no access to. A workflow that attempted it would
report "cannot judge" on every run — and a gate that is permanently unable to judge teaches
people to ignore red, which is worse than no gate. What *does* run in CI is
`check_upstream.py --self-test`, which builds synthetic repos and proves both drift
directions still work.

Run the real check wherever the source is checked out:

```
python3 check_upstream.py --source ../fd-infra-deploy
```

This is a judgement call worth stating plainly: an ungated check relies on someone
remembering, so it will eventually not run. That is accepted here because the alternative
— storing a token with read access to the private repo in a public repo — re-creates the
exact exposure these tools exist to find.

Exit: `0` in sync · `1` drift · `2` cannot judge (source repo or manifest unavailable).
