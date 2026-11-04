"""评测入口。用法(在 backend 目录用其虚拟环境运行):

    cd backend && uv run python ../evals/run_evals.py --label deepseek-v4 [--tag injection] [--case id]
    uv run python ../evals/run_evals.py --compare results/a.json results/b.json

结果写入 evals/results/<时间>-<label>.json,包含每个用例的规则检查、judge 评分和完整轨迹。
"""

import argparse
import json
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import yaml
from checks import run_checks
from judge import judge, judge_config
from layout import grade as grade_layout
from runner import Client, run_case

ROOT = Path(__file__).parent
RESULTS = ROOT / "results"


def load_cases(tag: str | None, only: list[str] | None, with_layout: bool = False) -> list[dict]:
    cases = []
    for f in sorted((ROOT / "cases").glob("*.yaml")):
        cases.extend(yaml.safe_load(f.read_text(encoding="utf-8")))
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "用例 id 重复"
    if tag:
        cases = [c for c in cases if tag in c.get("tags", [])]
    elif not only and not with_layout:
        # 排版用例贵(约占全量 token 的四分之一)且只在自检对照时有用,默认不跑
        cases = [c for c in cases if "layout" not in c.get("tags", [])]
    if only:
        cases = [c for c in cases if c["id"] in only]
    return cases


def memory_preflight(client: Client, concurrency: int) -> str | None:
    """记忆是全局的:要求实例已开启记忆、当前为空(否则会清掉真实记忆)、且串行运行。"""
    if concurrency != 1:
        return "记忆用例共享全局状态,必须 --concurrency 1"
    try:
        caps = client.capabilities()
    except Exception as exc:
        return f"读取 /capabilities 失败: {exc!r}(后端是否为新版本?)"
    if not caps.get("memory"):
        return "后端未开启长期记忆(MEMORY_ENABLED=true)"
    existing = client.list_memories()
    if existing:
        return f"实例里已有 {len(existing)} 条记忆;评测会清空记忆,请换一个空实例或先手动清理,以免丢失真实数据"
    return None


def model_warnings(health: dict, observed: list[str], judge_models: list[str], judge_cfg: dict | None) -> list[str]:
    """对照「配置的模型」和「响应里实际的模型」,返回告警。中转站会悄悄把请求转给别的模型,
    只看结果文件里的名字事后才发现,所以运行前后各检查一次。"""
    warns = []
    cfg = health.get("llm_config") or {}
    if cfg.get("warning"):
        warns.append(cfg["warning"])
    configured = cfg.get("model")
    if configured and observed and any(m != configured for m in observed):
        warns.append(f"Agent 配置的模型是 {configured},但实际响应的是 {observed}")
    if judge_cfg and judge_models and any(m != judge_cfg["model"] for m in judge_models):
        warns.append(f"judge 配置的模型是 {judge_cfg['model']},但实际响应的是 {judge_models}")
    return warns


def evaluate(client: Client, case: dict, timeout: float, use_judge: bool, use_layout: bool = False) -> dict:
    res, metrics = run_case(client, case, ROOT / "fixtures", timeout)
    checks = run_checks(res, case.get("checks", []))
    verdict = judge(case, res) if use_judge and case.get("rubric") else None
    layout = grade_layout(res) if use_layout and res.files else None
    rules_ok = all(c["passed"] for c in checks)
    judge_ok = True if verdict is None or verdict["passed"] is None else verdict["passed"]
    return {
        "id": case["id"],
        "category": case["category"],
        "tags": case.get("tags", []),
        "passed": rules_ok and judge_ok,
        "rules_passed": rules_ok,
        "checks": checks,
        "judge": verdict,
        "layout": layout,
        "answer": res.answer,
        "error": res.error,
        "output_files": sorted(res.files),
        **metrics,
    }


def layout_stats(rs: list[dict]) -> dict:
    """只统计有可评分产物的用例;没开 --layout 时返回空,不污染原有汇总。"""
    graded = [r for r in rs if r.get("layout") and r["layout"]["passed"] is not None]
    if not graded:
        return {}
    return {
        "layout_graded": len(graded),
        "layout_pass_rate": round(sum(r["layout"]["passed"] for r in graded) / len(graded), 3),
        "layout_major_total": sum(r["layout"]["major"] for r in graded),
    }


def summarize(results: list[dict]) -> dict:
    def agg(rs: list[dict]) -> dict:
        n = len(rs)
        toks = [r["usage"].get("total_tokens", 0) for r in rs if r["usage"]]
        return {
            "cases": n,
            "success_rate": round(sum(r["passed"] for r in rs) / n, 3) if n else None,
            "avg_tool_calls": round(statistics.mean(r["tool_calls"] for r in rs), 1) if n else None,
            "avg_tokens": round(statistics.mean(toks)) if toks else None,
            "avg_seconds": round(statistics.mean(r["seconds"] for r in rs), 1) if n else None,
            **layout_stats(rs),
        }

    cats = sorted({r["category"] for r in results})
    return {"overall": agg(results), "by_category": {c: agg([r for r in results if r["category"] == c]) for c in cats}}


def fmt(v, suffix=""):
    return "-" if v is None else f"{v}{suffix}"


def compare(paths: list[str]) -> str:
    runs = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    lines = ["| 标签 | 用例数 | 成功率 | 平均工具调用 | 平均 tokens | 平均耗时(s) |", "|---|---|---|---|---|---|"]
    for r in runs:
        o = r["summary"]["overall"]
        lines.append(
            f"| {r['label']} | {o['cases']} | {fmt(o['success_rate'] and round(o['success_rate'] * 100, 1), '%')} "
            f"| {fmt(o['avg_tool_calls'])} | {fmt(o['avg_tokens'])} | {fmt(o['avg_seconds'])} |"
        )
    if any("layout_graded" in r["summary"]["overall"] for r in runs):
        lines += ["", "排版(渲染后由多模态模型评分,只含有 docx/pptx/xlsx/pdf 产物的用例):", "", "| 标签 | 评分用例数 | 排版合格率 | major 问题总数 |", "|---|---|---|---|"]
        for r in runs:
            o = r["summary"]["overall"]
            rate = o.get("layout_pass_rate")
            lines.append(f"| {r['label']} | {fmt(o.get('layout_graded'))} | {fmt(rate is not None and round(rate * 100, 1), '%')} | {fmt(o.get('layout_major_total'))} |")
    cats = sorted({c for r in runs for c in r["summary"]["by_category"]})
    lines += ["", "按类别成功率:", "", "| 类别 | " + " | ".join(r["label"] for r in runs) + " |", "|---|" + "---|" * len(runs)]
    for c in cats:
        cells = []
        for r in runs:
            s = r["summary"]["by_category"].get(c)
            cells.append("-" if not s else f"{round(s['success_rate'] * 100)}% ({s['cases']})")
        lines.append(f"| {c} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--base-url", default=os.getenv("EVAL_BASE_URL", "http://localhost:5001"))
    ap.add_argument("--tag")
    ap.add_argument("--case", action="append")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--layout", action="store_true", help="对 docx/pptx/xlsx/pdf 产物渲染后让多模态模型评排版(需本机能 docker exec 进沙箱)")
    ap.add_argument("--compare", nargs="+")
    args = ap.parse_args()

    if args.compare:
        print(compare(args.compare))
        return 0

    api_key = os.getenv("EVAL_API_KEY") or (os.getenv("API_KEYS") or "").split(",")[0] or None
    client = Client(args.base_url, api_key)
    health = client.http.get("/health").json()
    cases = load_cases(args.tag, args.case, args.layout)
    if not cases:
        print("没有匹配的用例")
        return 2
    if any(c.get("memory") for c in cases):
        problem = memory_preflight(client, args.concurrency)
        if problem:
            print(f"无法运行记忆用例: {problem}")
            return 2
    use_judge = not args.no_judge and judge_config() is not None
    judge_desc = f"{judge_config()['protocol']}:{judge_config()['model']}" if use_judge else "off"
    print(f"后端健康: {health} | 用例 {len(cases)} | judge={judge_desc}")
    for w in model_warnings(health, [], [], None):
        print(f"[警告] {w}")

    def one(case: dict) -> dict:
        r = evaluate(client, case, args.timeout, use_judge, args.layout)
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"[{mark}] {r['id']:<28} {r['seconds']:>6}s tools={r['tool_calls']:<3} tokens={r['usage'].get('total_tokens', '-')}", flush=True)
        for c in r["checks"]:
            if not c["passed"]:
                print(f"       x {c['check']}: {c['detail']}")
        if r["judge"] and r["judge"]["passed"] is False:
            print(f"       x judge: {r['judge']['reason']}")
        lay = r["layout"]
        if lay and lay["passed"] is not None:
            print(f"       layout: major={lay['major']} minor={lay['minor']}")
        return r

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(one, cases))

    summary = summarize(results)
    observed = sorted({m for r in results for m in r["usage"].get("models", [])})
    judge_models = sorted(
        {(r["judge"] or {}).get("model") for r in results if r["judge"]}
        | {m for r in results for m in ((r["layout"] or {}).get("models") or [])}
    )
    judge_models = [m for m in judge_models if m]
    warnings = model_warnings(health, observed, judge_models, judge_config() if use_judge else None)
    for w in warnings:
        print(f"[警告] {w}")
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{datetime.now():%Y%m%d-%H%M%S}-{args.label}.json"
    out.write_text(
        json.dumps(
            {
                "label": args.label,
                # 以后端 usage 事件上报的实际模型为准(回退/命名配置时环境变量不可信)
                "models": observed,
                "configured_model": (health.get("llm_config") or {}).get("model"),
                "judge_models": judge_models,
                "model_warnings": warnings,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "judge_model": (judge_config() or {}).get("model") if use_judge else None,
                "summary": summary,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n{compare([str(out)])}\n\n结果: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
