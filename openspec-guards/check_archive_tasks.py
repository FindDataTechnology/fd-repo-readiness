#!/usr/bin/env python3
"""Archived changes with unticked tasks: unticked needs a recorded reason, new ones fail.

## What this catches

The archive skill already counts `- [ ]` and asks "continue?". The gap is that the answer
is not written down. Three shapes are indistinguishable from the directory state alone:

  1. the work was never done,
  2. the work was done but nobody ticked the box,
  3. the task is deliberately deferred.

Only the sentence written into the file separates them. So:

- unticked **with** a dated reason in the file → pass (print a warning line);
- unticked **without** a reason → hit (exit 1), naming the change and the count;
- fully ticked → pass;
- a third-state marker (`- [~]`, "deferred / reworked / in progress") also needs a
  recorded reason — it is neither ticked nor unticked, and a gate that only counts
  `- [ ]` reads it as "all done";
- no `tasks.md` → report the fact only (such changes carry other evidence);
- archive directory unreadable → **exit 2**, never read "not checked" as "all ticked".

## Why a baseline

Pre-existing archived changes with unticked tasks are real history. Failing all of them
makes a gate that is permanently red, and permanently red gates get ignored. The baseline
lists them, prints them, and does not count them; **new** unticked tasks still fail.
Baseline entries must carry a date and a reason, and the file should only shrink.

## What counts as a reason

Any of these **in the tasks.md body** (not in this script's comments):

  1. a line `归档豁免：<reason>` / `archive-exemption: <reason>` with a non-empty reason;
  2. a `## 归档豁免` / `## Archive exemption` heading followed by at least one non-empty
     line of body text;
  3. a `> **...结案...**` / `> **...closed...**` blockquote followed by non-empty body text.

All three require body text: a heading with nothing under it is someone writing four
words, not someone giving a reason.

## Usage

  python3 check_archive_tasks.py                  # scan cwd
  python3 check_archive_tasks.py --root /path     # scan another root
  python3 check_archive_tasks.py --no-baseline    # ignore baseline (red-proof)
  python3 check_archive_tasks.py --self-test      # fixtures only

Exit: 0 all pass / 1 new hits / 2 cannot judge (archive directory missing or unreadable).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

UNCHECKED = re.compile(r"^- \[ \]", re.M)
CHECKED = re.compile(r"^- \[[xX]\]", re.M)
# `- [~]` marks "reworked / deferred / in progress": neither ticked nor unticked, so a
# gate that counts only `- [ ]` sees it as done. Assuming there are only two states is
# itself the defect — the third state must also require a recorded reason.
PARTIAL = re.compile(r"^- \[(?![ xX])([^\]]?)\]", re.M)
# The markers are accepted in Chinese and English so the tool is usable as-is outside the
# project it came from. Keep both: dropping one silently turns recorded reasons into hits.
INLINE_WAIVER = re.compile(r"(?:归档豁免|archive[- ]exemption)\s*[：:]\s*(\S.*)$", re.M)
WAIVER_HEADING = re.compile(r"^#{2,}\s*(?:归档豁免|[Aa]rchive[- ][Ee]xemption)\s*$", re.M)
# Post-archive closure note: `> **6.1 closed (...)**：...`
CLOSURE_NOTE = re.compile(r"^>\s*\*\*[^\n]*?(结案|豁免|[Cc]losed|[Ee]xempt)[^\n]*?\*\*", re.M)


def has_waiver(text: str) -> tuple[bool, str]:
    """Return (has_waiver_with_reason, which_form)."""
    # HTML 注释先剥掉：注释里的「归档豁免」人看不见，渲染出来什么都没有，
    # 那不是留痕。不剥的话行内正则会把 `<!-- 归档豁免：… -->` 也认成理由
    # ——自证里那条臂最初就是这么写的，而断言只判「名字出现」⇒ 真假都绿。
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)

    m = INLINE_WAIVER.search(text)
    if m and m.group(1).strip():
        return True, "inline"

    for pattern, label in ((WAIVER_HEADING, "heading"), (CLOSURE_NOTE, "closure-note")):
        m = pattern.search(text)
        if not m:
            continue
        # 标题/引用块之后必须还有正文，否则只有四个字，不成理由。
        rest = text[m.end():]
        if any(ln.strip() and not ln.strip().startswith(("#", ">")) for ln in rest.splitlines()):
            return True, label
    return False, ""


def load_baseline(path: Path) -> dict[str, str]:
    """`<change-dir>  <date>  <reason>` per line; '#' comments and blanks skipped."""
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


def scan(root: Path, baseline: dict[str, str]) -> tuple[int, list[str], int]:
    """Return (hits, report_lines, population_scanned)."""
    archive = root / "openspec" / "changes" / "archive"
    if not archive.is_dir():
        raise FileNotFoundError(f"归档目录不存在：{archive}")

    lines: list[str] = []
    hits: list[str] = []
    scanned = 0
    no_tasks = 0
    clean = 0
    waived = 0
    baselined = 0

    for d in sorted(p for p in archive.iterdir() if p.is_dir()):
        scanned += 1
        tasks = d / "tasks.md"
        if not tasks.is_file():
            no_tasks += 1
            continue
        try:
            text = tasks.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise OSError(f"读不到 {tasks}: {exc}") from exc

        unchecked = len(UNCHECKED.findall(text))
        partials = PARTIAL.findall(text)
        if unchecked == 0 and not partials:
            clean += 1
            continue

        total = unchecked + len(CHECKED.findall(text)) + len(partials)
        marks = "".join(sorted({m or "?" for m in partials}))
        extra = f"，另有 {len(partials)} 个 [{marks}] 非完成标记" if partials else ""
        ok, form = has_waiver(text)
        if ok:
            waived += 1
            lines.append(f"  ⚠ {d.name}: {unchecked}/{total} 未勾{extra}，有留痕（{form}）")
            continue
        if d.name in baseline:
            baselined += 1
            reason = baseline[d.name]
            lines.append(f"  · {d.name}: {unchecked}/{total} 未勾{extra}，基线登记（{reason or '未写理由'}）")
            continue
        hits.append(d.name)
        lines.append(f"  ✗ {d.name}: {unchecked}/{total} 未勾{extra}，且文件里没有带日期的理由")

    summary = (
        f"扫描 {scanned} 个归档：全勾 {clean}／有未勾但有留痕 {waived}／"
        f"基线登记 {baselined}／无 tasks.md {no_tasks}／**新命中 {len(hits)}**"
    )
    return len(hits), lines + ["  " + summary], scanned


SELF_TEST_FIXTURES = {
    # 全勾 ⇒ 通过
    "2026-01-01-all-checked": "# T\n\n- [x] 1.1 done\n- [x] 1.2 done\n",
    # 未勾 + 无理由 ⇒ 命中
    "2026-01-02-no-reason": "# T\n\n- [ ] 1.1 todo\n- [x] 1.2 done\n",
    # 未勾 + 行内理由 ⇒ 通过
    "2026-01-03-inline": "# T\n\n- [ ] 1.1 等上游先合\n\n归档豁免：2026-01-03 等 add-x 先归档，行为已在 main\n",
    # 未勾 + 标题但无正文 ⇒ 命中（只有四个字不成理由）
    "2026-01-04-empty-heading": "# T\n\n- [ ] 1.1 todo\n\n## 归档豁免\n",
    # 未勾 + 标题 + 正文 ⇒ 通过
    "2026-01-05-heading-body": "# T\n\n- [ ] 1.1 todo\n\n## 归档豁免\n2026-01-05 前置未完成，见 proposal\n",
    # 未勾 + 结案引用块 ⇒ 通过
    "2026-01-06-closure": "# T\n\n- [ ] 6.1 顺序约束\n\n> **6.1 结案（2026-01-06）**：前置已满足，两个能力 strict rc=0。\n",
    # 未勾 + 基线登记 ⇒ 不计命中
    "2026-01-07-baselined": "# T\n\n- [ ] 1.1 todo\n",
    # 无 tasks.md ⇒ 事实
    "2026-01-08-no-tasks": None,
    # 只有 [~] 无理由 ⇒ 命中（第三态标记不能当「已勾」）
    "2026-01-11-only-partial": "# T\n\n- [x] 1.1 done\n- [~] 2.6 改道待落地\n",
    # 只有 [~] + 带日期理由 ⇒ 通过
    "2026-01-12-partial-waived": "# T\n\n- [~] 2.6 改道\n\n归档豁免：2026-01-12 改道随 add-y 落地，本 change 只改记录\n",
}


def _make_fixture_root(tmp: Path) -> Path:
    root = tmp / "ws"
    archive = root / "openspec" / "changes" / "archive"
    for name, body in SELF_TEST_FIXTURES.items():
        d = archive / name
        d.mkdir(parents=True)
        if body is not None:
            (d / "tasks.md").write_text(body, encoding="utf-8")
    base = tmp / "baseline.txt"
    base.write_text("2026-01-07-baselined  2026-01-07  历史遗留，行为已在 main\n", encoding="utf-8")
    return root


def self_test() -> int:
    failures = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal failures
        if cond:
            print(f"✓ {label}")
        else:
            failures += 1
            print(f"✗ {label}{(' :: ' + detail) if detail else ''}")

    tmp = Path(tempfile.mkdtemp(prefix="archive-tasks-"))
    try:
        root = _make_fixture_root(tmp)
        base = load_baseline(tmp / "baseline.txt")

        # ① 带基线：只有「无理由」的两条该命中
        hits, lines, scanned = scan(root, base)
        joined = "\n".join(lines)
        check("人口含全部 10 个夹具", scanned == 10, f"scanned={scanned}")
        check("带基线只命中无理由的 3 条", hits == 3, f"hits={hits}\n{joined}")
        check("[~] 无理由判红（第三态不能当已勾）", "✗ 2026-01-11-only-partial" in joined, joined)
        check("[~] + 理由通过", "⚠ 2026-01-12-partial-waived" in joined, joined)
        check("[~] 的报文点名标记字符", "[~] 非完成标记" in joined, joined)
        check("命中点名 no-reason", "2026-01-02-no-reason" in joined)
        check("命中点名 empty-heading（只有标题不算理由）", "2026-01-04-empty-heading" in joined)
        check("行内理由通过", "2026-01-03-inline" in joined and "inline" in joined)
        check("标题+正文通过", "2026-01-05-heading-body" in joined and "heading" in joined)
        check("结案引用块通过", "2026-01-06-closure" in joined and "closure-note" in joined)
        check("基线登记只报事实", "2026-01-07-baselined" in joined and "基线登记" in joined)
        check("全勾的不打印", "2026-01-01-all-checked" not in joined)
        check("无 tasks.md 的不打印", "2026-01-08-no-tasks" not in joined)
        check("汇总里人口与各档都有", "扫描 10 个归档" in joined and "无 tasks.md 1" in joined, joined)

        # ② 关掉基线：baselined 那条必须转红（证明基线是唯一放行口）
        hits_nb, lines_nb, _ = scan(root, {})
        check("关基线后 baselined 转红", hits_nb == 4 and "2026-01-07-baselined" in "\n".join(lines_nb))

        # ③ 归档目录不存在 ⇒ 抛 FileNotFoundError（调用方退 2，不装绿）
        try:
            scan(tmp / "nowhere", {})
            check("缺归档目录应判不出", False, "没有抛异常")
        except FileNotFoundError:
            check("缺归档目录应判不出", True)

        # ④ 理由行必须是**本文件正文**里的：把豁免写法放进 HTML 注释不许算
        d = root / "openspec" / "changes" / "archive" / "2026-01-09-commented"
        d.mkdir()
        (d / "tasks.md").write_text("# T\n\n- [ ] 1.1 todo\n\n<!-- 归档豁免：这里只是注释 -->\n", encoding="utf-8")
        # ⑤ 行内理由为空 ⇒ 命中（「写了这四个字」≠「给过理由」）
        d2 = root / "openspec" / "changes" / "archive" / "2026-01-10-empty-inline"
        d2.mkdir()
        (d2 / "tasks.md").write_text("# T\n\n- [ ] 1.1 todo\n\n归档豁免：   \n", encoding="utf-8")
        hits_c, lines_c, _ = scan(root, {})
        joined_c = "\n".join(lines_c)
        # 断言**判定**而不只是「名字出现」：⚠ 行与 ✗ 行都含 change 名，
        # 只判出现的话这条臂在两种实现下都绿（第一版就是这么写的）。
        check("注释里的豁免判红", "✗ 2026-01-09-commented" in joined_c, joined_c)
        check("空理由行判红", "✗ 2026-01-10-empty-inline" in joined_c, joined_c)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"自证：{'全绿' if failures == 0 else f'{failures} 条红'}")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="归档 change 未勾任务体检（只读）")
    ap.add_argument("--root", default=".",
                    help="repo root containing openspec/ (default: cwd)")
    ap.add_argument("--baseline", default=None, help="基线条目文件，缺省 scripts/archive-tasks-baseline.txt")
    ap.add_argument("--no-baseline", action="store_true", help="忽略基线（自证能红用）")
    ap.add_argument("--self-test", action="store_true", help="只跑夹具自证，不碰真实归档")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    root = Path(a.root).resolve()
    base_path = Path(a.baseline) if a.baseline else root / "scripts" / "archive-tasks-baseline.txt"
    baseline = {} if a.no_baseline else load_baseline(base_path)

    try:
        hits, lines, _ = scan(root, baseline)
    except (FileNotFoundError, OSError) as exc:
        print(f"判不出：{exc}", file=sys.stderr)
        return 2

    for line in lines:
        print(line)
    print()
    if hits:
        print(f"结论：{hits} 个归档带未勾任务且没有留痕。")
        print("修法两条：① 真做过就去复跑并勾上（判据在每条任务自己的「验证=」里）；")
        print("          ② 刻意延后/顺序约束就写一句带日期的理由（`归档豁免：<日期> <理由>`）。")
        return 1
    print("结论：未勾任务都有留痕，或已在基线里登记。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
