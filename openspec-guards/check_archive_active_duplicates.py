#!/usr/bin/env python3
"""Detect an OpenSpec change that exists both in `changes/` and in `changes/archive/`.

## What this catches

`openspec archive` merges a change's delta into `specs/` and then **moves** the directory
into `archive/`. If that flow only half-runs — manual copy, an archived change dragged
back, a merge from an old branch that brings the live copy along — the same delta exists
in two places at once. Every later `openspec archive <name>` then **appends** the same
requirements to the capability spec rather than replacing them. Neither `openspec
validate` nor `--strict` complains, because each copy is individually valid.

The failure disguises itself: the visible symptom is often "the spec is broken" (a live
copy with no `specs/` at all and an emptied delta), not "there are two copies".

## What counts

A live directory `openspec/changes/<name>` alongside
`openspec/changes/archive/[<date>-]<name>`. Both namings are recognised; the date prefix
is a convention, not a requirement.

Hits split into two tiers because the remedy differs:

- **subset** (the live copy adds nothing the archived one lacks) — mechanical: delete the
  live copy. Comparison normalises `- [x]` / `- [ ]` / `- [~]` to `- ` first, so a tick
  difference is not treated as a content difference. Without that normalisation, copies
  differing only by checkbox state read as *divergent* and the gate degrades into "a human
  must look at every one".
- **divergent** (the live copy has lines or files the archive lacks) — needs judgement:
  merge that content into the archived `tasks.md` as a record, then delete the live copy;
  or, if this is genuinely a same-name continuation, rename it to a new change.

`.openspec.yaml` is metadata (`skip_specs` and friends): differences there are reported
but never counted as divergent.

## What does not count

- Archive only, no live directory — the normal case.
- Live directory with no archived twin — a change in flight, normal.
- Anything listed in the baseline file — printed line by line, not counted. The baseline
  records pre-existing debt and is only allowed to shrink.

## Usage

  python3 check_archive_active_duplicates.py                  # scan cwd
  python3 check_archive_active_duplicates.py --root /path     # scan another root
  python3 check_archive_active_duplicates.py --no-baseline    # ignore baseline (red-proof)
  python3 check_archive_active_duplicates.py --self-test      # fixtures only, no real archive

Exit: 0 no hits / 1 hits found / 2 cannot judge (`openspec/changes/` not readable).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

# 归一「勾选状态」：三种写作（已勾/未勾/改道）在内容比较上等价。
CHECKBOX = re.compile(r"^(\s*)[-*+]\s+\[[ xX~]\]\s*")
META_FILES = {".openspec.yaml"}
UNCHECKED = re.compile(r"^\s*[-*+]\s+\[ \]", re.M)

# 自证臂数；写死在注释里的数字会漂，所以由脚本自己核。
SELF_TEST_ARMS = 13


def load_baseline(path: Path) -> dict[str, str]:
    """`<change>  <date>  <reason>` per line; '#' comments and blanks skipped."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        out[parts[0]] = parts[2] if len(parts) > 2 else ""
    return out


def _norm(text: str) -> list[str]:
    """归一后的非空行（保留顺序，用于「活副本独有」的判断）。"""
    out: list[str] = []
    for raw in text.splitlines():
        line = CHECKBOX.sub(r"\1- ", raw).rstrip()
        if line.strip():
            out.append(line)
    return out


def _files(d: Path) -> dict[str, Path]:
    if not d.is_dir():
        return {}
    return {str(p.relative_to(d)): p for p in sorted(d.rglob("*")) if p.is_file()}


def compare(active: Path, archived: Path) -> tuple[str, list[str], int, int]:
    """Return (verdict, notes, active_unchecked, archive_unchecked)."""
    a_files = _files(active)
    r_files = _files(archived)
    notes: list[str] = []
    divergent = False

    for rel, ap in a_files.items():
        if rel in META_FILES:
            if rel not in r_files:
                notes.append(f"元数据 {rel} 只在活副本（不算内容差异）")
            elif ap.read_bytes() != r_files[rel].read_bytes():
                notes.append(f"元数据 {rel} 与归档不同（不算内容差异）")
            continue
        rp = r_files.get(rel)
        if rp is None:
            divergent = True
            notes.append(f"活副本有 `{rel}`，归档里没有这个文件")
            continue
        a_lines = _norm(ap.read_text(encoding="utf-8", errors="replace"))
        r_set = set(_norm(rp.read_text(encoding="utf-8", errors="replace")))
        unique = [ln for ln in a_lines if ln not in r_set]
        if unique:
            divergent = True
            for ln in unique[:5]:
                notes.append(f"`{rel}` 活副本独有：{ln.strip()[:110]}")
            if len(unique) > 5:
                notes.append(f"`{rel}` 活副本另有 {len(unique) - 5} 行独有内容（未逐条列出）")

    for rel in r_files:
        if rel in META_FILES:
            continue
        if rel not in a_files:
            notes.append(f"归档有 `{rel}`、活副本没有（活副本更旧，删它不会丢这条）")

    def unchecked(d: Path) -> int:
        p = d / "tasks.md"
        if not p.is_file():
            return -1
        return len(UNCHECKED.findall(p.read_text(encoding="utf-8", errors="replace")))

    return ("divergent" if divergent else "subset"), notes, unchecked(active), unchecked(archived)


def _twin(archive: Path, name: str) -> Path | None:
    """归档侧同名目录：`archive/<name>` 或 `archive/<YYYY-MM-DD>-<name>`。"""
    exact = archive / name
    if exact.is_dir():
        return exact
    dated = sorted(p for p in archive.glob(f"*-{name}") if p.is_dir())
    return dated[-1] if dated else None


def scan(root: Path, baseline: dict[str, str]) -> tuple[int, list[str]]:
    changes = root / "openspec" / "changes"
    archive = changes / "archive"
    if not changes.is_dir():
        raise FileNotFoundError(f"{changes} 不存在")
    if not archive.is_dir():
        raise FileNotFoundError(f"{archive} 不存在（归档目录看不见＝判不出，不是『没有并存』）")

    actives = sorted(p for p in changes.iterdir() if p.is_dir() and p.name != "archive")
    lines: list[str] = []
    hits = 0
    subset = divergent = baselined = 0

    for d in actives:
        twin = _twin(archive, d.name)
        if twin is None:
            continue
        if d.name in baseline:
            baselined += 1
            lines.append(f"· {d.name} ← {twin.name}：基线登记（{baseline[d.name] or '无理由'}）")
            continue
        verdict, notes, a_unc, r_unc = compare(d, twin)
        if verdict == "subset":
            subset += 1
        else:
            divergent += 1
        hits += 1
        mark = "⚠" if verdict == "subset" else "✗"
        lines.append(f"{mark} {d.name} ← {twin.name}（{verdict}）")
        if a_unc >= 0 or r_unc >= 0:
            fmt = lambda n: "无 tasks.md" if n < 0 else f"{n} 个未勾"
            lines.append(f"    任务：活副本 {fmt(a_unc)} / 归档 {fmt(r_unc)}")
        for n in notes:
            lines.append(f"    {n}")
        if verdict == "subset":
            lines.append("    处置：删活副本 `git rm -r openspec/changes/%s`（归档已是权威，删它不丢信息）" % d.name)
        else:
            lines.append("    处置：先把上面几行的内容并进归档那份当留痕，再删活副本；")
            lines.append("          若这是「同名续开」，改成一个新的 change 名（同名再归档＝双加）")

    head = (f"扫描 {len(actives)} 个活 change：{subset + divergent + baselined} 个有归档同名"
            f"（subset {subset} / divergent {divergent}）；基线登记 {baselined}")
    return hits, [head] + lines


def _make_fixture_root(tmp: Path) -> Path:
    root = tmp / "ws"
    changes = root / "openspec" / "changes"
    archive = changes / "archive"

    def put(base: Path, name: str, files: dict[str, str]) -> None:
        for rel, body in files.items():
            p = base / name / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")

    # ① 只有在途 change，无归档同名 ⇒ 不命中
    put(changes, "solo-change", {"tasks.md": "- [ ] 1.1 做事\n", "proposal.md": "# p\n"})
    # ② 只有归档 ⇒ 不命中（正常态）
    put(archive, "2026-01-01-closed-change", {"tasks.md": "- [x] 1.1 做事\n"})
    # ③ 逐字相同的并存 ⇒ subset
    same = {"tasks.md": "- [x] 1.1 做事\n", "proposal.md": "# p\n"}
    put(changes, "exact-dup", same)
    put(archive, "2026-01-02-exact-dup", same)
    # ④ 唯一差异是勾选状态 ⇒ 必须判 subset（归一承重）
    put(changes, "checkbox-dup", {"tasks.md": "- [ ] 1.1 做事\n- [ ] 1.2 又做事\n"})
    put(archive, "2026-01-03-checkbox-dup",
        {"tasks.md": "- [x] 1.1 做事\n- [x] 1.2 又做事\n## 4. 实跑读数\n- 归档后 405 → 428 行\n"})
    # ⑤ 活副本有归档没有的行 ⇒ divergent，且要点名那行
    put(changes, "divergent-dup", {"tasks.md": "- [ ] 1.1 旧写法\n- [ ] 9.9 只在活副本里的活\n"})
    put(archive, "2026-01-04-divergent-dup", {"tasks.md": "- [x] 1.1 旧写法\n"})
    # ⑥ 活副本有归档没有的文件 ⇒ divergent
    put(changes, "extra-file-dup", {"tasks.md": "- [x] 1.1 a\n", "specs/cap/spec.md": "## ADDED Requirements\n"})
    put(archive, "2026-01-05-extra-file-dup", {"tasks.md": "- [x] 1.1 a\n"})
    # ⑦ 归档目录不带日期前缀 ⇒ 也要认
    put(changes, "undated-dup", {"tasks.md": "- [x] 1.1 a\n"})
    put(archive, "undated-dup", {"tasks.md": "- [x] 1.1 a\n"})
    # ⑧ .openspec.yaml 差异不判 divergent（元数据）
    put(changes, "meta-only-dup", {"tasks.md": "- [x] 1.1 a\n", ".openspec.yaml": "skip_specs: false\n"})
    put(archive, "2026-01-06-meta-only-dup", {"tasks.md": "- [x] 1.1 a\n", ".openspec.yaml": "skip_specs: true\n"})
    # ⑨ 基线条目 ⇒ 不计命中
    put(changes, "baselined-dup", {"tasks.md": "- [x] 1.1 a\n"})
    put(archive, "2026-01-07-baselined-dup", {"tasks.md": "- [x] 1.1 a\n"})
    # ⑩ - [~] 第三态与已勾等价（归一要认它）
    put(changes, "tilde-dup", {"tasks.md": "- [~] 1.1 改道\n"})
    put(archive, "2026-01-08-tilde-dup", {"tasks.md": "- [x] 1.1 改道\n"})

    # ⑪ 归档比活副本多一个文件 ⇒ 仍 subset，但要有一条「活副本更旧」的证据行
    put(changes, "archive-extra-file-dup", {"tasks.md": "- [x] 1.1 a\n"})
    put(archive, "2026-01-09-archive-extra-file-dup",
        {"tasks.md": "- [x] 1.1 a\n", "design.md": "# 设计\n"})

    base = tmp / "baseline.txt"
    base.write_text("baselined-dup  2026-01-01  纪律建立前的老账\n", encoding="utf-8")
    return root


def self_test() -> int:
    failures = 0
    arms = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal failures, arms
        arms += 1
        if cond:
            print(f"✓ {label}")
        else:
            failures += 1
            print(f"✗ {label}{(' :: ' + detail) if detail else ''}")

    tmp = Path(tempfile.mkdtemp(prefix="archive-active-dup-"))
    try:
        root = _make_fixture_root(tmp)
        base = load_baseline(tmp / "baseline.txt")
        hits, lines = scan(root, base)
        joined = "\n".join(lines)

        check("人口：扫到 10 个活 change", "扫描 10 个活 change" in joined, joined[:400])
        check("命中 8 条（baselined 与 solo 不计）", hits == 8, f"hits={hits}\n{joined[:400]}")
        check("逐字相同的并存判 subset", "⚠ exact-dup" in joined and "（subset）" in joined, joined[:400])
        check("唯一差异是勾选状态仍判 subset（归一承重）",
              "⚠ checkbox-dup ← 2026-01-03-checkbox-dup（subset）" in joined, joined[:400])
        check("归档比活副本多文件 ⇒ 仍 subset，并点名「活副本更旧」的证据行",
              "⚠ archive-extra-file-dup" in joined and "归档有 `design.md`、活副本没有" in joined,
              joined[:400])
        check("活副本独有行判 divergent 并逐字点名",
              "✗ divergent-dup" in joined and "9.9 只在活副本里的活" in joined, joined[:400])
        check("活副本多出文件判 divergent 并点名文件名",
              "✗ extra-file-dup" in joined and "`specs/cap/spec.md`" in joined, joined[:400])
        check("归档目录不带日期前缀也认", "⚠ undated-dup ← undated-dup" in joined, joined[:400])
        check(".openspec.yaml 差异只报事实、不判 divergent",
              "⚠ meta-only-dup" in joined and "元数据 .openspec.yaml 与归档不同" in joined, joined[:400])
        check("基线条目逐条打印且不计命中",
              "· baselined-dup" in joined and hits == 8, joined[:400])
        check("- [~] 第三态与已勾等价", "⚠ tilde-dup ← 2026-01-08-tilde-dup（subset）" in joined, joined[:400])
        check("汇总行分开数 subset / divergent",
              "subset 6 / divergent 2" in joined and "基线登记 1" in joined, joined[:400])

        # 判不出：归档目录看不见
        bare = tmp / "bare"
        (bare / "openspec" / "changes").mkdir(parents=True)
        try:
            scan(bare, {})
            check("归档目录看不见 ⇒ 抛错（不是静默 0 命中）", False, "没有抛错")
        except FileNotFoundError as exc:
            check("归档目录看不见 ⇒ 抛错（不是静默 0 命中）", "archive" in str(exc), str(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    check(f"臂数与 SELF_TEST_ARMS 一致（{SELF_TEST_ARMS}）", arms == SELF_TEST_ARMS, f"实跑 {arms} 臂")
    print(f"自证：{'全绿' if failures == 0 else f'{failures} 条红'}")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="归档/活目录同名 change 体检（只读）")
    ap.add_argument("--root", default=".",
                    help="repo root containing openspec/ (default: cwd)")
    ap.add_argument("--baseline", default=None,
                    help="基线条目文件，缺省 scripts/archive-duplicate-baseline.txt")
    ap.add_argument("--no-baseline", action="store_true", help="忽略基线（自证能红用）")
    ap.add_argument("--self-test", action="store_true", help="只跑夹具自证，不碰真实归档")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    root = Path(a.root).resolve()
    base_path = Path(a.baseline) if a.baseline else root / "scripts" / "archive-duplicate-baseline.txt"
    baseline = {} if a.no_baseline else load_baseline(base_path)

    try:
        hits, lines = scan(root, baseline)
    except (FileNotFoundError, OSError) as exc:
        print(f"判不出：{exc}", file=sys.stderr)
        return 2

    for line in lines:
        print(line)
    print()
    if hits:
        print(f"结论：{hits} 个 change 同时存在于归档与活目录。")
        print("为什么必须处理：再跑一次 `openspec archive <name>` 会把同一批 requirement")
        print("**追加**进 capability spec（不是覆盖），而 validate 与归档未勾闸都不会响。")
        return 1
    print("结论：没有归档/活目录同名的 change。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
