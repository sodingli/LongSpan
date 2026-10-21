"""灌入演示任务，让工作台一打开就有东西可看、可动手。

三个状态各一个，覆盖 M0.5 的完整故事线：

  C. 停在关卡的任务        → 可亲手体验「确认 / 我要改一改」
  B. 已升级为精修的任务     → 可看到「草稿续跑」如何复用产物、不重来
  A. 跑完的草稿 ★默认打开   → 草稿诚实性卡片：用时 / 差在哪 / 继续优化入口

创建顺序经过安排：列表按创建时间倒序，A 最后创建因此排在最前 —— 默认落在它上面。

用法： python scripts/seed_demo.py [api_base]
默认 api_base = http://127.0.0.1:5173/api （走前端代理）
"""

import sys
import time

import httpx

API = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5173/api").rstrip("/")
cli = httpx.Client(trust_env=False, timeout=30.0)


def create(title: str, brief: dict, channel: str = "fast") -> str:
    r = cli.post(f"{API}/runs", json={"skill_id": "comic-drama", "title": title,
                                      "brief": brief, "channel": channel})
    r.raise_for_status()
    return r.json()["id"]


def wait_finish(run_id: str, auto_approve: bool, label: str) -> dict:
    for _ in range(200):
        time.sleep(0.3)
        d = cli.get(f"{API}/runs/{run_id}").json()
        if auto_approve:
            for g in d["gates"]:
                if g["status"] == "pending":
                    cli.post(f"{API}/runs/{run_id}/gates/{g['id']}/resolve",
                             json={"status": "approved", "feedback": ""})
        if d["status"] in ("succeeded", "failed") or (not auto_approve and d["status"] == "awaiting_gate"):
            draft = d.get("draft") or {}
            extra = ""
            if draft.get("ttfv_seconds") is not None:
                extra = f"，首个成品 {draft['ttfv_seconds']}s"
            print(f"  [{label}] {d['status']:14} 产物 {len(d['files'])} 个，"
                  f"决策 {len(d['decisions'])} 条，阶段 {len(d['steps'])} 个{extra}")
            return d
    print(f"  [{label}] 超时")
    return {}


def main() -> None:
    print("▸ C. 停在关卡的任务（深夜面馆）—— 等你亲手确认剧本")
    c = create("示例 · 深夜面馆（等你确认剧本）",
               {"idea": "只在凌晨两点开门的面馆，每位客人都要用一个秘密换一碗面",
                "genre": "治愈日常", "style": "日系清新"})
    wait_finish(c, auto_approve=False, label="C")

    print("▸ B. 已升级为精修的任务（废土机械师）—— 看续跑如何复用草稿产物")
    b = create("示例 · 废土机械师（草稿已升级精修）",
               {"idea": "末日废土里的机械维修师，捡到一株会说话的营养液管子，它记得战前的世界",
                "style": "厚涂写实", "genre": "科幻"})
    wait_finish(b, auto_approve=True, label="B·草稿")
    up = cli.post(f"{API}/runs/{b}/upgrade")
    if up.status_code == 200:
        print(f"      升级精修 → 只补跑 {up.json().get('pending_stages')}")
        wait_finish(b, auto_approve=True, label="B·精修")
    else:
        print(f"      升级失败：{up.status_code} {up.text[:120]}")

    print("▸ A. 跑完的草稿（隐藏的美食评论家）★ 默认打开这个")
    a = create("示例 · 隐藏的美食评论家（草稿已就绪）",
               {"idea": "外卖小哥其实是隐藏的美食评论家，被米其林主厨当众约战"})
    wait_finish(a, auto_approve=True, label="A")

    print("\n完成。打开 http://localhost:5173 查看。")


if __name__ == "__main__":
    main()
