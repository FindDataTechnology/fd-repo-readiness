#!/usr/bin/env python3
# ── dup-image-tags: 找出「多个不可变 tag 指向同一 digest」的镜像 ────────────────
#
# WHY (2026-11-18)：通用出片池此前每推一次快照就铸一个 `sha-<commit>`，不论内容变
# 没变。实测某镜像的 17 个 sha tag 里已有 2 组是同一 digest 的两个 tag
# （sha-4ac020c/sha-fad3c26 同为 sha256:8407f73b）。
#
# 代价不是磁盘，是**配额**：TCR 个人版每仓 100 tag，满了之后所有 push 被拒；且
# **v2 API 删除不释放配额，只有控制台删除才回收**（2026-10-06 实证）。所以清理必须
# 人工在控制台做，而人工需要一份「删哪个」的清单——这就是本工具唯一的产出。
#
# 它只读。MUST NOT 删除任何东西：`skopeo delete` 那类 v2 调用删了也不释放配额，
# 做了等于没做还留一个「已清理」的错觉。
#
# 用法：
#   dup-image-tags <registry-ns/image>            # 如 myorg/my-image
#   dup-image-tags --registry hkccr ...           # 换 registry（默认两个都查）
#   dup-image-tags --json
#   dup-image-tags --self-test
#
# 需要 skopeo + 该 registry 的凭据（env TCR_USER / TCR_PASS，或在本机 cheap3 上跑）。
#
# 退出码：0 有结论（含「无重复」）｜2 判不出（无凭据 / 网络 / 镜像不存在）
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict

DEFAULT_REGISTRIES = ("hkccr.ccs.tencentyun.com", "ccr.ccs.tencentyun.com")
CFG_BENIGN_PREFIX = ("main", "latest")


def sh(args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 2, f"{type(e).__name__}: {e}"


def creds_args() -> list[str]:
    u, p = os.environ.get("TCR_USER"), os.environ.get("TCR_PASS")
    return ["--creds", f"{u}:{p}"] if u and p else []


def list_tags(ref: str) -> tuple[list[str] | None, str]:
    rc, out = sh(["skopeo", "list-tags", *creds_args(), f"docker://{ref}"])
    if rc != 0:
        return None, out.strip()[:200]
    try:
        return json.loads(out).get("Tags") or [], ""
    except json.JSONDecodeError as e:
        return None, f"list-tags 输出不可解析：{e}"


def digest_of(ref: str) -> str | None:
    rc, out = sh(["skopeo", "inspect", *creds_args(), f"docker://{ref}"])
    if rc != 0:
        return None
    try:
        return json.loads(out).get("Digest")
    except json.JSONDecodeError:
        return None


def groups(ref: str, tags: list[str]) -> tuple[dict[str, list[str]], int]:
    """按 digest 分组不可变 tag。返回 (重复组, 检查失败的 tag 数)。"""
    by_digest: dict[str, list[str]] = defaultdict(list)
    failed = 0
    for t in tags:
        if t.startswith(CFG_BENIGN_PREFIX) or t == "main":
            continue
        d = digest_of(f"{ref}:{t}")
        if d is None:
            failed += 1
            continue
        by_digest[d].append(t)
    return {d: sorted(ts) for d, ts in by_digest.items() if len(ts) > 1}, failed


def main() -> int:
    ap = argparse.ArgumentParser(
        description="dup-image-tags: 列出多个 tag 指向同一 digest 的镜像")
    ap.add_argument("image", nargs="?", help="<namespace>/<image>，如 myorg/my-image")
    ap.add_argument("--registry", action="append", help="registry 主机（默认 hkccr + ccr 都查）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.image:
        ap.error("需要 <namespace>/<image>")

    regs = args.registry or DEFAULT_REGISTRIES
    report: dict = {"image": args.image, "registries": {}, "rc": 0}

    for reg in regs:
        ref = f"{reg}/{args.image}"
        tags, why = list_tags(ref)
        if tags is None:
            report["registries"][reg] = {"error": why}
            continue
        dup, failed = groups(ref, tags)
        report["registries"][reg] = {
            "tags": len(tags),
            "immutable": len([t for t in tags if not t.startswith(CFG_BENIGN_PREFIX) and t != "main"]),
            "duplicates": [{"digest": d, "tags": ts, "keep": ts[-1]} for d, ts in dup.items()],
            "unreadable": failed,
        }

    readable = [r for r, v in report["registries"].items() if "error" not in v]
    if not readable:
        report["rc"] = 2
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("  · 判不出：两个 registry 都读不到（凭据 / 网络 / 镜像不存在）", file=sys.stderr)
            for reg, v in report["registries"].items():
                print(f"    {reg}: {v['error']}", file=sys.stderr)
        return 2

    report["reclaimable"] = sum(
        len(g["tags"]) - 1 for v in report["registries"].values() for g in v.get("duplicates", []))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    for reg, v in report["registries"].items():
        print(f"\n=== {reg}/{args.image}")
        if "error" in v:
            print(f"  · 判不出：{v['error']}")
            continue
        print(f"  tag {v['tags']} 个，其中不可变 {v['immutable']} 个"
              + (f"，{v['unreadable']} 个读不到 digest" if v["unreadable"] else ""))
        if not v["duplicates"]:
            print("  ✓ 无重复（没有多个 tag 指向同一 digest）")
            continue
        print(f"  ⚠ {len(v['duplicates'])} 组重复：")
        for g in v["duplicates"]:
            print(f"    {g['digest'][:19]}  ← {len(g['tags'])} 个 tag")
            for t in g["tags"]:
                mark = "  ← 建议保留" if t == g["keep"] else ""
                print(f"      {t}{mark}")

    print(f"\n可回收 {report['reclaimable']} 个 tag（同一 digest 多出来的那些）。")
    print("⚠ 清理只能在镜像仓库的控制台做：v2 API 删除**不释放配额**")
    print("  （2026-10-06 实证：skopeo 真删、digest 404 都已证实，配额仍不减）。")
    print("  本工具**只读**，没有删除任何东西。")
    return 0


def self_test() -> int:
    """分组的红绿双证 + 只读自证，全部离线。"""
    ok = True

    def expect(name: str, cond: bool) -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  {'✓' if cond else '✗'} {name}")

    orig = globals()["digest_of"]

    # ① 两个 tag 同 digest → 一组
    fake = {"a": "sha256:111", "b": "sha256:111", "c": "sha256:222", "main": "sha256:999"}
    globals()["digest_of"] = lambda ref: fake.get(ref.split(":")[-1])
    g, failed = groups("r/ns/img", list(fake))
    expect("① 同 digest 的两个 tag 归为一组", len(g) == 1 and g["sha256:111"] == ["a", "b"])
    expect("② main 不算不可变 tag", all("main" not in ts for ts in g.values()))
    expect("③ 无读不出的 tag", failed == 0)

    # ④ 全不同 digest → 无重复
    for k in fake:
        fake[k] = f"sha256:{k}"
    globals()["digest_of"] = lambda ref: fake.get(ref.split(":")[-1])
    g2, _ = groups("r/ns/img", list(fake))
    expect("④ digest 全不同 → 无重复组", g2 == {})

    # ⑤ 读不出 digest 的计入 unreadable，不误判成重复
    def some_none(ref):
        t = ref.split(":")[-1]
        return None if t == "x" else fake.get(t)
    globals()["digest_of"] = some_none
    g3, failed3 = groups("r/ns/img", ["x", "a", "b"])
    expect("⑤ 读不出的 tag 不参与分组且被计数", failed3 == 1 and "x" not in str(g3))

    # ⑥ 只读：分组过程不调用任何写操作
    calls = []
    globals()["sh"] = lambda args, timeout=60: (calls.append(args), (0, '{"Tags":[]}'))[1]
    list_tags("r/ns/img")
    expect("⑥ list_tags 只调 list-tags，不调 delete",
           calls and all("delete" not in " ".join(c) for c in calls))

    globals()["digest_of"] = orig
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
