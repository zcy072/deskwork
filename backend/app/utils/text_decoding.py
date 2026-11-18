"""把文件字节解码成文本,兼容中文 Windows 常见的 GBK 编码。"""


def decode_text(data: bytes) -> str:
    """先严格按 UTF-8(去掉 BOM);不是合法 UTF-8 再试 GB18030(GBK/GB2312 的超集)。

    GB18030 几乎能解码任意字节,所以只在内容"看起来像中文"时采用,
    否则按 UTF-8 替换非法字节,免得把二进制或其他编码解成一片生僻汉字。
    """
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode("gb18030")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "　" <= ch <= "〿")
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    if non_ascii and cjk / non_ascii >= 0.6:
        return text
    return data.decode("utf-8", errors="replace")
