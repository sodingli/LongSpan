"""HTTP 端到端验证：走真实接口，含 SSE 事件流与关卡确认。

不经过代理（trust_env=False），避免本机 HTTP_PROXY 干扰。
"""

import json
import sys
import threading
import time

import httpx

API = "http://127.0.0.1:8000/api"
cli = httpx.Client(trust_env=False, timeout=20.0)

errors: list[str] = []
sse_events: list[dict] = []
sse_state = {"connected": False, "msg": "", "closed": False}


def sse_reader(run_id: str) -> None:
    """独立线程消费 SSE，验证真实推送与断线续传能力。"""
    try:
        with cli.stream("GET", f"{API}/runs/{run_id}/events") as resp:
            sse_state["connected"] = resp.status_code == 200
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    sse_state["msg"] = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    try:
                        sse_events.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError:
                        pass
                if sse_state.get("done"):
                    break
    except Exception as e:  # noqa: BLE001
        sse_state["error"] = str(e)
    finally:
        sse_state["closed"] = True


def main() -> int:
    print("=" * 72)
    print("① 创建任务（快通道，零输入）")
    r = cli.post(f"{API}/runs", json={"skill_id": "comic-drama", "channel": "fast",
                                      "title": "HTTP 端到端验证"})
    r.raise_for_status()
    run = r.json()
    run_id = run["id"]
    print(f"   run_id = {run_id}")
    print(f"   brief 已自动补默认值: {list(run['brief'].keys())}")

    # 先起 SSE 消费线程
    t = threading.Thread(target=sse_reader, args=(run_id,), daemon=True)
    t.start()
    time.sleep(0.6)
    print(f"② SSE 连接: {'已建立' if sse_state['connected'] else '失败'}")

    print("③ 轮询执行状态，遇到关卡自动确认")
    t0 = time.monotonic()
    final = None
    gate_seen = 0
    for _ in range(1500):
        time.sleep(0.35)
        d = cli.get(f"{API}/runs/{run_id}").json()
        for g in d["gates"]:
            if g["status"] == "pending":
                gate_seen += 1
                print(f"   ◆ 关卡挂起：{g['title']} → 自动确认")
                cli.post(f"{API}/runs/{run_id}/gates/{g['id']}/resolve",
                         json={"status": "approved", "feedback": "e2e 自动确认"})
        if d["status"] in ("succeeded", "failed", "cancelled"):
            final = d
            break

    elapsed = time.monotonic() - t0
    print("=" * 72)
    if final is None:
        print("✘ 超时未结束")
        return 1

    print(f"④ 结果")
    print(f"   状态     : {final['status']}")
    print(f"   耗时     : {elapsed:.2f}s")
    print(f"   进度     : {final['progress']:.0%}")
    print(f"   阶段     : " + ", ".join(f"{s['stage_name']}[{s['status']}]" for s in final["steps"]))
    print(f"   产物     : {len(final['files'])} 个 → {', '.join(final['files'])}")
    print(f"   决策留痕 : {len(final['decisions'])} 条（模型 {len([x for x in final['decisions'] if x['actor']=='model'])}）")
    print(f"   关卡     : {len(final['gates'])} 个，共挂起 {gate_seen} 次")
    cps = final["checkpoints"]
    print(f"   检查点   : {len(cps)} 条 → "
          + ", ".join(f"{c['status']}" for c in cps))

    print(f"⑤ SSE 收到事件 {len(sse_events)} 条；"
          f"类型覆盖: {sorted({e.get('type') for e in sse_events})[:8]}…")

    # 校验点
    checks = [
        ("跑完流水线", final["status"] == "succeeded"),
        ("有产物落盘", len(final["files"]) >= 5),
        ("含分镜图", any("panels/" in f for f in final["files"])),
        ("关卡挂起并被恢复", len(final["gates"]) >= 1 and final["gates"][0]["status"] == "approved"),
        ("决策留痕含模型判断", any(x["actor"] == "model" for x in final["decisions"])),
        ("SSE 推送了事件", len(sse_events) > 10),
        ("SSE 覆盖阶段事件", any(e.get("type") == "stage.completed" for e in sse_events)),
        ("SSE 覆盖关卡事件", any(e.get("type") == "gate.opened" for e in sse_events)),
        ("检查点已归档 in_progress", not any(c["status"] == "in_progress" for c in cps)),
        ("产物可下载", cli.get(f"{API}/runs/{run_id}/download").status_code == 200),
        ("单文件可读", cli.get(f"{API}/runs/{run_id}/files/script.md").status_code == 200),
    ]

    # ---------------------------------------------------------------- #
    # ⑦ M0.5：草稿诚实性 —— 北极星指标可计量 + 差距摊开说
    # ---------------------------------------------------------------- #
    draft = final.get("draft") or {}
    print("=" * 72)
    print("⑦ 草稿元数据")
    print(f"   是否草稿        : {draft.get('is_draft')}")
    print(f"   首次成品用时    : {draft.get('ttfv_seconds')}s"
          f"（目标 {draft.get('target_seconds')}s，达标={draft.get('within_target')}）")
    print(f"   未覆盖阶段      : {[s['name'] for s in draft.get('unrun_stages', [])]}")
    print(f"   技能声明差距    : {len(draft.get('gaps', []))} 条")
    for g in draft.get("gaps", []):
        print(f"      · {g}")
    print(f"   可继续优化      : {draft.get('can_upgrade')}")

    checks += [
        ("草稿被如实标注", draft.get("is_draft") is True),
        ("首次成品用时已计量", isinstance(draft.get("ttfv_seconds"), (int, float))),
        ("用时与目标可比较", draft.get("within_target") is not None),
        ("技能声明了草稿差距", len(draft.get("gaps") or []) > 0),
        ("列出了未覆盖阶段", any(s["id"] == "characters" for s in draft.get("unrun_stages") or [])),
        ("提供继续优化入口", draft.get("can_upgrade") is True),
        ("SSE 覆盖草稿就绪事件", any(e.get("type") == "draft.ready" for e in sse_events)),
    ]

    # ---------------------------------------------------------------- #
    # ⑧ 草稿 → 精修：续跑语义（复用已完成阶段，不重做）
    # ---------------------------------------------------------------- #
    print("=" * 72)
    print("⑧ 升级为精修通道（验证续跑语义）")
    before = {s["stage_id"]: s["attempt"] for s in final["steps"]}
    before_done = {k for k, s in before.items()}

    up = cli.post(f"{API}/runs/{run_id}/upgrade")
    print(f"   升级接口: {up.status_code} → {up.json()}")
    up.raise_for_status()

    final2 = None
    for _ in range(1500):
        time.sleep(0.35)
        d = cli.get(f"{API}/runs/{run_id}").json()
        for g in d["gates"]:
            if g["status"] == "pending":
                cli.post(f"{API}/runs/{run_id}/gates/{g['id']}/resolve",
                         json={"status": "approved", "feedback": "e2e 自动确认"})
        if d["status"] in ("succeeded", "failed", "cancelled"):
            final2 = d
            break
    if final2 is None:
        print("✘ 升级后超时未结束")
        return 1

    after = {s["stage_id"]: s for s in final2["steps"]}
    reruns = [k for k in before_done if after.get(k, {}).get("attempt", 0) > before[k]]
    newly = [k for k in after if k not in before_done]

    print(f"   状态            : {final2['status']} / 通道 {final2['channel']}")
    print(f"   阶段            : " + ", ".join(f"{s['stage_name']}[{s['status']}]" for s in final2["steps"]))
    print(f"   新补跑阶段      : {newly}")
    print(f"   被重跑的老阶段  : {reruns or '无（产物被复用）'}")
    print(f"   产物            : {len(final2['files'])} 个")

    # 事件流一直订阅到升级跑完，才能验证续跑事件确实推给了前端
    time.sleep(1.2)
    sse_state["done"] = True
    print(f"   SSE 累计事件    : {len(sse_events)} 条"
          f"（含续跑 {len([e for e in sse_events if e.get('type') == 'run.resumed'])} 条）")

    checks += [
        ("通道已切到精修", final2["channel"] == "slow"),
        ("只补跑未完成阶段", set(newly) == {"characters", "compose"}),
        ("复用了草稿产物未重跑", not reruns),
        ("精修后全阶段完成", all(s["status"] == "completed" for s in final2["steps"])),
        ("草稿产物未丢失", len(final2["files"]) >= len(final["files"])),
        ("SSE 覆盖续跑事件", any(e.get("type") == "run.resumed" for e in sse_events)),
        ("升级后不再是草稿", (final2.get("draft") or {}).get("is_draft") is False),
    ]

    print("=" * 72)
    print("⑥ 验收清单")
    for name, ok in checks:
        print(f"   {'✔' if ok else '✘'} {name}")
        if not ok:
            errors.append(name)

    print("=" * 72)
    print("✔ HTTP 端到端全部通过" if not errors else f"✘ 失败项: {errors}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
