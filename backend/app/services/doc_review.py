"""产物自检:把 docx/pptx/xlsx/pdf 渲染成页面图片,交给多模态模型挑排版问题。

渲染在沙箱内完成(LibreOffice + pdftoppm,沙箱断网也能跑),图片落在会话的 scratchpad,
审阅用的模型由 REVIEW_LLM_PROFILE 指定(必须支持图片输入)。
"""

import asyncio
import base64
import shlex
import shutil
import uuid
from pathlib import Path

from langchain_core.messages import HumanMessage

from app.agents.llms import get_named_model
from app.config import settings
from app.sandbox import CONTAINER_MANAGER
from app.services.image_view import message_text
from app.services.tool_feedback import ToolFeedbackError
from app.utils import logger

OFFICE_SUFFIXES = {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".odt", ".odp", ".ods"}
SUPPORTED_SUFFIXES = OFFICE_SUFFIXES | {".pdf"}
MAX_PAGES = 8
RENDER_DPI = 70
MAX_PAGE_BYTES = 10 * 1024 * 1024
SOFFICE_SCRIPT = "/workspace/skills/docx/scripts/office/soffice.py"
# 沙箱执行的上限就是 90 秒(EXEC_TIMEOUT_SEC),写大了也会被截断
RENDER_TIMEOUT_SEC = 90

REVIEW_PROMPT = """你是文档排版审阅员。下面是一份刚生成的{kind}的页面截图(共 {pages} 页,按顺序)。
用户的原始需求:{request}

请只指出**肉眼可见、需要修复的问题**,例如:文字溢出或被截断、重叠、表格列被挤压或超出页面、
大片异常空白、图片/图表缺失或变形、字号过小难以阅读、乱码或缺字(方块)、标题与正文层级混乱、页间不一致。
不要评价内容是否完整、是否正确,不要建议增加章节或内容,不要提风格偏好;
内容简短、页面留白多本身不是问题。拿不准的不要报,宁可 PASS。

输出格式:
- 第一行写 "VERDICT: PASS" 或 "VERDICT: FIX"
- 若为 FIX,逐条列出问题,每条写明页码、位置、现象和建议的修复方式;最多 8 条,按严重程度排序
- 若为 PASS,用一句话说明即可"""


def review_enabled() -> bool:
    return bool(settings.REVIEW_LLM_PROFILE.strip())


async def render_pages(session_id: str, virtual_path: str) -> list[bytes]:
    """返回渲染出的页面图片(JPEG 字节,按页序)。出错抛 ToolFeedbackError,信息直接给模型看。"""
    session = await CONTAINER_MANAGER.get_or_create(session_id)
    virtual_path = session.resolve_virtual_path(virtual_path)
    suffix = Path(virtual_path).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ToolFeedbackError(f"不支持审阅 {suffix or '无扩展名'} 文件,仅支持 docx/pptx/xlsx/pdf")
    if not await session.is_file(session.paths.virtual_to_container_path(virtual_path)):
        raise ToolFeedbackError(f"File not found: {virtual_path}")

    # 每次调用一个独立目录:并行审阅时互不清掉、读错对方的页面;读完即删,不占配额也不进快照
    run_id = uuid.uuid4().hex
    work = f"{session.paths.virtual_scratchpad}/_review/{run_id}"
    pdf = f"{work}/{Path(virtual_path).stem}.pdf"
    src = shlex.quote(virtual_path)
    steps = [f"mkdir -p {work}"]
    if suffix == ".pdf":
        steps.append(f"cp {src} {shlex.quote(pdf)}")
    else:
        steps.append(
            f"python3 {SOFFICE_SCRIPT} --headless --convert-to pdf --outdir {work} {src} >/dev/null 2>&1"
        )
    steps.append(
        f"pdftoppm -jpeg -r {RENDER_DPI} -f 1 -l {MAX_PAGES} {shlex.quote(pdf)} {work}/page"
    )
    host_dir = session.paths.scratchpad / "_review" / run_id
    try:
        out, is_error = await session.execute(" && ".join(steps), timeout=RENDER_TIMEOUT_SEC)
        if is_error:
            raise ToolFeedbackError(f"渲染失败: {out[:500]}")
        pages = await asyncio.to_thread(lambda: sorted(host_dir.glob("page-*.jpg")))
        if not pages:
            raise ToolFeedbackError("渲染失败:没有生成任何页面图片(文件可能已损坏)")
        try:
            # 图片在 agent 可写的 scratchpad 里,读取时不能跟随它换上的符号链接
            return await asyncio.to_thread(
                lambda: [session.paths.read_host_file(p, MAX_PAGE_BYTES) for p in pages]
            )
        except (OSError, ValueError) as exc:
            raise ToolFeedbackError("读取页面图片失败") from exc
    finally:
        await asyncio.to_thread(shutil.rmtree, host_dir, True)


async def review_pages(pages: list[bytes], kind: str, request: str) -> str:
    profile, model = get_named_model(settings.REVIEW_LLM_PROFILE.strip())
    content: list[dict] = [
        {
            "type": "text",
            "text": REVIEW_PROMPT.format(
                kind=kind, pages=len(pages), request=request or "(未提供)"
            ),
        }
    ]
    for page in pages:
        b64 = base64.b64encode(page).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    logger.info("文档自检: %d 页,审阅模型 %s", len(pages), profile.model)
    result = await model.ainvoke([HumanMessage(content=content)])
    return message_text(result.content)
