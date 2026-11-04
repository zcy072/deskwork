"""长文档检索:分块、中英文分词、BM25 排序。"""

from app.services.doc_search import Bm25Index, format_results, split_chunks, tokenize


def test_tokenize_mixed():
    toks = tokenize("青鸟 budget 2,368,000")
    assert {"青", "鸟", "青鸟", "budget", "2", "368", "000"} <= set(toks)


def test_split_keeps_offsets_and_size():
    text = "\n".join(f"第{i}段 " + "内容" * 40 for i in range(30))
    chunks = split_chunks(text, size=200)
    assert len(chunks) > 5
    assert all(len(c.text) <= 200 + 5 for c in chunks)
    for c in chunks:
        assert text[c.start : c.start + 5] == c.text[:5]
    assert [c.id for c in chunks] == list(range(len(chunks)))


def test_split_hard_cuts_long_line():
    chunks = split_chunks("x" * 1200, size=500)
    assert [len(c.text) for c in chunks] == [500, 500, 200]


def _doc():
    filler = [f"第{i}段:本季度例行工作正常推进,无特别事项。" for i in range(200)]
    filler[120] = "青鸟试点项目的最终预算为 2,368,000 元,负责人周明远。"
    filler[40] = "备用供应商是北辰精工,交付周期 47 天。"
    return "\n".join(filler)


def test_bm25_finds_needles():
    index = Bm25Index(split_chunks(_doc(), size=300))
    top = index.search("青鸟 预算", 3)
    assert "2,368,000" in top[0][0].text
    top = index.search("备用供应商 交付周期", 3)
    assert "北辰精工" in top[0][0].text


def test_bm25_no_match_and_format():
    index = Bm25Index(split_chunks(_doc(), size=300))
    assert index.search("紫荆", 3) == []
    assert "没有找到" in format_results([], len(index.chunks))
    out = format_results(index.search("青鸟", 2), len(index.chunks))
    assert "周明远" in out and "段" in out


def test_empty_document():
    assert Bm25Index(split_chunks("")).search("a") == []
