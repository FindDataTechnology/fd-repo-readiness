#!/usr/bin/env python3
"""Capabilities declared by archived deltas that no live spec knows about.

## What this catches

`openspec archive` merges a change's delta **into** `specs/`. Only then does the
capability appear in `openspec list --specs` and get picked up by audits that check
whether specs still match code. When that merge does not happen — or something deletes
the result later — **nothing complains**: the change directory sits in `archive/`,
`openspec validate` does not look there, and the requirements live only inside the
archived delta file. Not in the index, not in any audit, and nobody knows it exists.

A formal retirement is a different thing from a loss, and the two must be
distinguishable:

- a retirement record (an archived delta carrying `## REMOVED Requirements` for it)
  passes — the spec was deliberately deleted, the text is still in the delta, the
  decision is reversible and findable;
- an archived `specs/<cap>/` directory with no `spec.md`, or one with zero
  requirements, is reported as a fact only — usually an empty leftover directory, and
  no requirement disappears because of it.

The check verifies by **content**, not just by name: for each hit it looks up every
`### Requirement:` heading in the live specs, and names where it landed if it turns up
elsewhere (`heading landed in <other cap>`) — that is a rename or a merge, not a loss.

## Why a baseline

Most pre-existing orphans predate the discipline that would have caught them. Failing all
of them makes a permanently red gate, and permanently red gates get ignored. The baseline
lists them, prints them, and does not count them; **new** orphans still fail. Entries
carry a date and a reason, and the file should only shrink.

## Usage

  python3 check_orphan_capabilities.py                 # scan cwd
  python3 check_orphan_capabilities.py --root /path    # scan another root
  python3 check_orphan_capabilities.py --no-baseline   # ignore baseline (red-proof)
  python3 check_orphan_capabilities.py --self-test     # fixtures only

Exit: 0 no new orphans / 1 new orphans (or a live spec referencing an unlisted one) /
2 cannot judge.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

REQ_TITLE = re.compile(r"^### Requirement:\s*(.+)$", re.M)
REMOVED = re.compile(r"^## REMOVED Requirements", re.M)


def load_baseline(path: Path) -> dict[str, str]:
    """`<capability>  <date>  <reason>` per line; '#' comments and blanks skipped."""
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


def _refs(text: str, cap: str) -> tuple[bool, bool]:
    """活 spec 正文对某个 capability 名的引用强度：(强, 弱)。

    **Why two tiers**: the same string in spec prose can be one of three things — a
    capability name, a repo/tool name, or an infrastructure object name. Splitting strong
    from weak references keeps a repo name from being read as a capability reference.
    The word-boundary trap is worth stating: a spec whose own name contains the orphan's
    name would otherwise report "this spec references the orphan" when the thing being
    referenced is itself.

    ⇒ 强证据只认**路径指针**（`openspec/specs/<cap>` / `specs/<cap>`）：那是在说
    「规格在这儿」，而那儿什么也没有。其余整词命中只作弱提示，不改变判定。
    整词判定用 `(?<![\\w-])…(?![\\w-])`：连字符两侧不算词边界。
    """
    strong = re.search(rf"(?:openspec/)?specs/{re.escape(cap)}(?![\w-])", text) is not None
    weak = re.search(rf"(?<![\w-]){re.escape(cap)}(?![\w-])", text) is not None
    return strong, weak


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def scan(root: Path, baseline: dict[str, str]) -> tuple[int, list[str]]:
    """Return (hits, report_lines)."""
    specs_dir = root / "openspec" / "specs"
    archive = root / "openspec" / "changes" / "archive"
    if not specs_dir.is_dir():
        raise FileNotFoundError(f"spec 目录不存在：{specs_dir}")
    if not archive.is_dir():
        raise FileNotFoundError(f"归档目录不存在：{archive}")

    live: dict[str, set[str]] = {}          # capability -> 需求标题（小写）
    live_texts: list[tuple[str, str]] = []  # (capability, 全文) 供悬空引用检索
    for d in sorted(p for p in specs_dir.iterdir() if p.is_dir()):
        f = d / "spec.md"
        if not f.is_file():
            continue
        text = _read(f)
        live[d.name] = {t.strip().lower() for t in REQ_TITLE.findall(text)}
        live_texts.append((d.name, text))

    declared: dict[str, list[str]] = {}   # capability -> [change]
    titles: dict[str, list[str]] = {}     # capability -> [requirement titles]
    retired: dict[str, list[str]] = {}    # capability -> [change with REMOVED]
    empty_dirs: list[str] = []
    # Non-canonical layouts: capability -> note. Two shapes exist in practice:
    # (1) a flat `specs/<cap>.spec.md` file — `openspec archive` only reads
    #     `specs/<cap>/spec.md`, so this content was never merged in. Enumerating by
    #     directory made the whole change invisible to this check; a large delta could
    #     carry hundreds of lines and be scanned by nothing.
    # (2) an empty `specs/<cap>/` directory — recorded as an empty-delta fact. It is not
    #     a declaration, and there is no content to lose.
    shape_note: dict[str, str] = {}
    for d in sorted(p for p in archive.iterdir() if p.is_dir()):
        sd = d / "specs"
        if not sd.is_dir():
            continue
        for entry in sorted(sd.iterdir()):
            if entry.is_file():
                # flat 文件：cap 名取自文件名（剥掉 `.md`，再剥可能的 `.spec`）
                if entry.suffix != ".md":
                    empty_dirs.append(f"{d.name}/specs/{entry.name}（非 .md，未解析）")
                    continue
                cap = entry.name[: -len(".md")]
                if cap.endswith(".spec"):
                    cap = cap[: -len(".spec")]
                text = _read(entry)
                found = [t.strip() for t in REQ_TITLE.findall(text)]
                declared.setdefault(cap, []).append(d.name)
                titles.setdefault(cap, []).extend(found)
                if REMOVED.search(text):
                    retired.setdefault(cap, []).append(d.name)
                n_lines = text.count("\n") + (0 if text.endswith("\n") else 1)
                n_sec = len(re.findall(r"^###\s+\S", text, re.M))
                shape_note[cap] = (
                    f"delta 是 **flat 文件** `{entry.name}`（{n_lines} 行、{n_sec} 个 `### ` 小节、"
                    f"{len(found)} 条 `### Requirement:`）——`openspec archive` 只读 "
                    f"`specs/<cap>/spec.md`，这份内容从未被合入"
                )
                continue
            cap = entry.name
            f = entry / "spec.md"
            if not f.is_file():
                empty_dirs.append(f"{d.name}/specs/{cap}（无 spec.md）")
                continue
            text = _read(f)
            found = [t.strip() for t in REQ_TITLE.findall(text)]
            if not found:
                empty_dirs.append(f"{d.name}/specs/{cap}（0 条需求，{len(text.strip())} 字节正文）")
                continue
            declared.setdefault(cap, []).append(d.name)
            titles.setdefault(cap, []).extend(found)
            if REMOVED.search(text):
                retired.setdefault(cap, []).append(d.name)

    lines: list[str] = []
    hits: list[str] = []
    ok_live = sum(1 for c in declared if c in live)
    ok_retired = sum(1 for c in declared if c not in live and c in retired)

    for cap in sorted(declared):
        if cap in live:
            continue
        if cap in retired:
            lines.append(
                f"  · {cap}: 已退役（{retired[cap][0]} 的 `## REMOVED Requirements`）——"
                f"「删了」有据，不是丢了"
            )
            continue

        # 标题是否落在别的 capability（改名/合并）——按内容判，不只看名字
        moved = sorted({
            other for t in {x.lower() for x in titles[cap]}
            for other, ts in live.items() if t in ts
        } - {cap})
        # 悬空引用分强弱：强＝正文里有 `specs/<cap>` 路径指针；弱＝只出现整词
        refs = [(other, *_refs(text, cap)) for other, text in live_texts]
        dangling_strong = sorted(o for o, s, _w in refs if s)
        dangling_weak = sorted(o for o, s, w in refs if w and not s)
        extra = ""
        if moved:
            extra += f"；标题落在 {', '.join(moved)}（疑似改名/合并，需人工确认）"
        if dangling_strong:
            extra += f"；**活 spec 指着 `specs/{cap}`**：{', '.join(dangling_strong)}"
        elif dangling_weak:
            extra += f"；同名整词出现在 {', '.join(dangling_weak)}（可能是仓/对象名，非引用）"
        detail = f"{len(titles[cap])} 条需求，声明于 {declared[cap][0]}"
        if cap in shape_note:
            detail += f"；{shape_note[cap]}"

        if cap in baseline:
            lines.append(f"  ⚠ {cap}: {detail}{extra}，基线登记（{baseline[cap] or '未写理由'}）")
            continue
        hits.append(cap)
        lines.append(f"  ✗ {cap}: {detail}{extra}，specs/ 里没有且无退役记录")

    for e in empty_dirs:
        lines.append(f"  · 空 delta：{e} —— 没有需求会因此消失，只报事实")

    orphan_titles = sum(len(titles[c]) for c in declared
                        if c not in live and c not in retired)
    summary = (
        f"扫描 {len(declared)} 个被声明的 capability：活 spec {ok_live}／"
        f"已退役 {ok_retired}／基线登记 {sum(1 for c in declared if c not in live and c not in retired and c in baseline)}／"
        f"**新孤儿 {len(hits)}**；只存在于归档 delta 的需求标题 {orphan_titles} 条；"
        f"空 delta {len(empty_dirs)} 个；非规范布局 {len(shape_note)} 个"
    )
    return len(hits), lines + ["  " + summary]


SELF_TEST_FIXTURES = {
    # (capability, 声明 change, spec.md 正文)
    "live-cap": ("2026-01-01-a", "# live-cap\n\n## ADDED Requirements\n\n### Requirement: Thing one\n"),
    "retired-cap": ("2026-01-02-b", "# retired-cap\n\n## REMOVED Requirements\n\n### Requirement: Gone\n"),
    "orphan-cap": ("2026-01-03-c",
                   "# orphan-cap\n\n## ADDED Requirements\n\n"
                   "### Requirement: Orphan requirement one\n\n"
                   "### Requirement: Orphan requirement two\n"),
    "baselined-cap": ("2026-01-04-d", "# baselined-cap\n\n## ADDED Requirements\n\n### Requirement: Old one\n"),
    "renamed-cap": ("2026-01-05-e", "# renamed-cap\n\n## ADDED Requirements\n\n### Requirement: Shared title\n"),
}

LIVE_SPECS = {
    "live-cap": "# live-cap\n\n### Requirement: Thing one\n",
    # 强证据：正文里有路径指针 `specs/orphan-cap` ⇒ 指着不存在的规格
    "user-of-orphan": "# user-of-orphan\n\nSee `openspec/specs/orphan-cap` for the contract.\n\n### Requirement: Uses it\n",
    # 弱证据：只出现整词（可能是仓名/对象名，不是 capability 引用）
    "mentions-baselined": "# mentions-baselined\n\nThe baselined-cap runner CronJob.\n\n### Requirement: Names it\n",
    # 假红探针：活 spec **自己的名字**以孤儿名开头（`renamed-cap` 是 `renamed-cap-target` 的前缀），
    # 旧版 `\b` 词边界会把它算成「活 spec 引用了孤儿」
    "renamed-cap-target": "# renamed-cap-target\n\n### Requirement: Shared title\n",
}


def _make_fixture_root(tmp: Path) -> Path:
    root = tmp / "ws"
    archive = root / "openspec" / "changes" / "archive"
    specs = root / "openspec" / "specs"
    for cap, (change, body) in SELF_TEST_FIXTURES.items():
        d = archive / change / "specs" / cap
        d.mkdir(parents=True)
        (d / "spec.md").write_text(body, encoding="utf-8")
    for cap, body in LIVE_SPECS.items():
        d = specs / cap
        d.mkdir(parents=True)
        (d / "spec.md").write_text(body, encoding="utf-8")
    # 空 delta 目录（有目录没 spec.md）
    (archive / "2026-01-06-f" / "specs" / "empty-dir").mkdir(parents=True)
    # 0 条需求的 delta 文件
    d = archive / "2026-01-07-g" / "specs" / "zero-req"
    d.mkdir(parents=True)
    (d / "spec.md").write_text("# zero-req\n\n## ADDED Requirements\n", encoding="utf-8")
    # 非规范布局 ①：flat delta 文件 `specs/<cap>.spec.md`（真实存在，见
    # 2026-08-05-mcp-server-for-fd-scraw-harness）。按目录枚举时它整个不存在 ⇒
    # 12 个工具的契约一条判据都扫不到。
    (archive / "2026-01-09-i" / "specs").mkdir(parents=True)
    (archive / "2026-01-09-i" / "specs" / "flat-orphan.spec.md").write_text(
        "# Flat orphan\n\n### discover_sources\n\n- keyword\n\n### analyze_page\n",
        encoding="utf-8",
    )
    # 非规范布局 ②：flat delta 但指向已有活 spec ⇒ 布局怪异不该变成假红
    (archive / "2026-01-10-j" / "specs").mkdir(parents=True)
    (archive / "2026-01-10-j" / "specs" / "live-cap.spec.md").write_text(
        "# Flat live\n\n### some_tool\n", encoding="utf-8"
    )
    base = tmp / "baseline.txt"
    base.write_text("baselined-cap  2026-01-04  历史遗留，实现已删\n", encoding="utf-8")
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

    tmp = Path(tempfile.mkdtemp(prefix="orphan-caps-"))
    try:
        root = _make_fixture_root(tmp)
        base = load_baseline(tmp / "baseline.txt")
        hits, lines = scan(root, base)
        joined = "\n".join(lines)

        check("人口：6 个被声明的 capability 都数到（含 1 个 flat 文件）", "扫描 6 个被声明" in joined, joined)
        check("带基线命中 orphan-cap / renamed-cap / flat-orphan 三条", hits == 3, f"hits={hits}\n{joined}")
        check("flat delta 文件被算成声明（不再整个 change 隐身）",
              "✗ flat-orphan:" in joined and "**flat 文件**" in joined, joined)
        check("flat 文件名剥掉 `.spec` 后缀（不是 `flat-orphan.spec`）",
              "flat-orphan.spec:" not in joined and "flat-orphan:" in joined, joined)
        check("flat 布局的证据给出行数与小节数（0 条 Requirement 也要说不出丢的是什么）",
              "个 `### ` 小节、0 条 `### Requirement:`" in joined, joined)
        check("flat delta 指向已有活 spec 不判红", "✗ live-cap" not in joined, joined)
        check("逐字点名 orphan-cap（不是靠计数）", "✗ orphan-cap:" in joined, joined)
        check("orphan-cap 的报文带需求条数 2", "orphan-cap: 2 条需求" in joined, joined)
        check("标题落在别处仍判红（改名要人确认，不许自动放行）",
              "✗ renamed-cap:" in joined, joined)
        check("活 spec 存在的判通过", "✗ live-cap" not in joined, joined)
        check("有退役记录的判通过并说明「删了不是丢了」",
              "· retired-cap: 已退役" in joined, joined)
        check("基线登记只报事实", "⚠ baselined-cap:" in joined and "基线登记" in joined, joined)
        check("强悬空引用点名到具体 spec", "活 spec 指着 `specs/orphan-cap`**：user-of-orphan" in joined, joined)
        check("弱命中不冒充引用", "同名整词出现在 mentions-baselined" in joined, joined)
        # 假红探针：renamed-cap 是活 spec 名 renamed-cap-target 的前缀，不许算成引用
        check("以孤儿名开头 ≠ 引用它（沿用了 `\\b` 就会假红）",
              "renamed-cap: 1 条需求，声明于 2026-01-05-e；标题落在" in joined
              and "指着 `specs/renamed-cap`" not in joined
              and "同名整词出现在 renamed-cap-target" not in joined, joined)
        check("标题落在别处（改名）被点名", "标题落在 renamed-cap-target" in joined, joined)
        check("空 delta 目录只报事实", "· 空 delta" in joined and "empty-dir" in joined, joined)
        check("0 条需求的 delta 只报事实", "zero-req" in joined and "0 条需求" in joined, joined)
        # 孤儿需求标题数 = orphan-cap 2 + renamed-cap 1 + baselined-cap 1（基线只压命中，不改口径）
        check("只存在于归档的需求标题数含基线条目（2+1+1=4；flat 文件 0 条不进这个数）",
              "需求标题 4 条" in joined, joined)
        check("非规范布局个数进汇总行", "非规范布局 2 个" in joined, joined)

        # 关基线：baselined-cap 必须转红（证明基线是唯一放行口）
        hits_nb, lines_nb = scan(root, {})
        check("关基线后 baselined-cap 转红且共 4 条",
              hits_nb == 4 and "✗ baselined-cap:" in "\n".join(lines_nb), f"hits={hits_nb}")

        # 判不出：缺目录不许装绿
        for missing in ("specs", "archive"):
            bad = tmp / f"bad-{missing}"
            (bad / "openspec").mkdir(parents=True)
            other = "changes/archive" if missing == "specs" else "specs"
            (bad / "openspec" / other).mkdir(parents=True)
            try:
                scan(bad, {})
                check(f"缺 {missing} 目录应判不出", False, "没有抛异常")
            except FileNotFoundError:
                check(f"缺 {missing} 目录应判不出", True)

        # 同一个 capability 被两个 change 声明 ⇒ 只报一次，且点名第一个
        d = root / "openspec" / "changes" / "archive" / "2026-01-08-h" / "specs" / "orphan-cap"
        d.mkdir(parents=True)
        (d / "spec.md").write_text("# orphan-cap\n\n## ADDED Requirements\n\n### Requirement: Extra\n", encoding="utf-8")
        hits2, lines2 = scan(root, base)
        joined2 = "\n".join(lines2)
        check("重复声明只算一次命中（仍是 3 条，不是 4）", hits2 == 3, f"hits={hits2}")
        check("重复声明的需求条数合并计数（2+1=3）", "orphan-cap: 3 条需求" in joined2, joined2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"自证：{'全绿' if failures == 0 else f'{failures} 条红'}")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="归档 delta 的孤儿 capability 体检（只读）")
    ap.add_argument("--root", default=".",
                    help="repo root containing openspec/ (default: cwd)")
    ap.add_argument("--baseline", default=None,
                    help="基线条目文件，缺省 scripts/orphan-capability-baseline.txt")
    ap.add_argument("--no-baseline", action="store_true", help="忽略基线（自证能红用）")
    ap.add_argument("--self-test", action="store_true", help="只跑夹具自证，不碰真实归档")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    root = Path(a.root).resolve()
    base_path = Path(a.baseline) if a.baseline else root / "scripts" / "orphan-capability-baseline.txt"
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
        print(f"结论：{hits} 个 capability 只在归档 delta 里，specs/ 里没有、也没有退役记录。")
        print("修法三条：① 该有 ⇒ 把它合进 specs/（重开一个 change 走 archive）；")
        print("          ② 不该有 ⇒ 补一个带 `## REMOVED Requirements` 的退役 change（原文留档）；")
        print("          ③ 已改名 ⇒ 在基线里写明落点，并把活 spec 里的悬空引用改成新名字。")
        return 1
    print("结论：没有新的孤儿 capability（历史条目见基线）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
