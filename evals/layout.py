"""排版评分:把产物渲染成页面图片,让多模态模型挑肉眼可见的排版问题。

和智能体自带的 review_document 是两套独立实现(不同提示词、按严重程度分级),
评测用它来量化"开启产物自检"前后的排版合格率。渲染在沙箱容器里完成(LibreOffice + pdftoppm)。
局限:评分模型默认就是 judge 模型,和被测的审阅模型可能是同一个,存在同源偏差。
"""

import base64
import io
import json
import os
import re
import subprocess
import tarfile

from checks import RunResult
from judge import complete, judge_config

SANDBOX = os.getenv("EVAL_SANDBOX_CONTAINER", "sandbox")
OFFICE = {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"}
MAX_PAGES = 8
DPI = 70

SYSTEM = """你是文档排版质检员。输入是一份文档的页面截图(按页序)。只看排版,不评价内容对错或多少。
把肉眼可见的问题分级列出:
- major(影响阅读或使用,必须修):
  * 文字被截断或溢出页面、内容互相重叠
  * 表格列被挤得过窄,导致单元格文字一个字一行地竖排、数字或单词被拆到两行,读起来很费力
  * 表格或图片超出页面边界
  * 乱码或方块字、图表缺失或严重变形、整页异常空白
- minor(不影响阅读):对齐略乱、间距不均、字号偏小但可读、风格不统一、页面下方留白较多
拿不准的不要报;但只要符合 major 的描述,就必须标 major,不要因为"还能勉强看懂"而降级。
只输出一个 JSON:{"issues":[{"page":页码,"severity":"major或minor","desc":"一句话"}]},没有问题就输出 {"issues":[]}。"""


def render(name: str, data: bytes) -> list[bytes]:
    """返回页面 JPEG 列表;不支持的类型返回空列表,渲染失败抛 RuntimeError。"""
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix not in OFFICE and suffix != ".pdf":
        return []
    src = f"in{suffix}"
    convert = "" if suffix == ".pdf" else f"HOME=$W soffice -env:UserInstallation=file://$W/lo --headless --convert-to pdf --outdir $W {src} >/dev/null 2>&1 && "
    script = (
        f"W=$(mktemp -d) && cat > $W/{src} && cd $W && {convert}"
        f"pdftoppm -jpeg -r {DPI} -f 1 -l {MAX_PAGES} in.pdf page && tar c -C $W page-*.jpg; rm -rf $W"
    )
    proc = subprocess.run(
        ["docker", "exec", "-i", SANDBOX, "sh", "-c", script], input=data, capture_output=True, timeout=180
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"渲染失败: {proc.stderr.decode(errors='replace')[:300]}")
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        members = sorted((m for m in tar.getmembers() if m.isfile()), key=lambda m: m.name)
        return [tar.extractfile(m).read() for m in members]


def parse_issues(text: str) -> list[dict] | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        issues = json.loads(m.group(0)).get("issues", [])
    except (ValueError, AttributeError):
        return None
    return [i for i in issues if isinstance(i, dict) and i.get("severity") in ("major", "minor")]


def grade(res: RunResult) -> dict:
    """对 output/ 下每个可渲染产物打分。无可渲染产物时 passed 为 None(不计入排版合格率)。"""
    cfg = judge_config()
    if cfg is None:
        return {"passed": None, "reason": "未配置 judge,已跳过", "files": {}}
    report: dict[str, dict] = {}
    models: set[str] = set()
    for name, data in res.files.items():
        try:
            pages = render(name, data)
        except Exception as exc:
            report[name] = {"error": str(exc)}
            continue
        if not pages:
            continue
        parts = [{"type": "text", "text": f"共 {len(pages)} 页:"}] + [
            {"type": "image", "media": "image/jpeg", "data": base64.b64encode(p).decode()} for p in pages
        ]
        try:
            text, actual = complete(cfg, SYSTEM, parts, timeout=180)
            models.add(actual)
            issues = parse_issues(text)
        except Exception as exc:
            report[name] = {"error": f"评分调用失败: {exc!r}"[:300]}
            continue
        report[name] = {"pages": len(pages), "issues": issues} if issues is not None else {"error": "评分输出无法解析"}
    graded = {n: r for n, r in report.items() if "issues" in r}
    if not graded:
        errored = [n for n, r in report.items() if "error" in r]
        return {"passed": None, "reason": "没有可评分的产物" + (f"(失败: {errored})" if errored else ""), "files": report}
    major = sum(1 for r in graded.values() for i in r["issues"] if i["severity"] == "major")
    minor = sum(1 for r in graded.values() for i in r["issues"] if i["severity"] == "minor")
    return {"passed": major == 0, "major": major, "minor": minor, "files": report, "models": sorted(models)}
