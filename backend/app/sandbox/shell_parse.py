"""bash 命令的轻量解析:剔除 heredoc 正文、按 shell 语法切成命令。

命令校验器(sandbox)和风险判定(agents/risk)共用。同样是尽力而为,不是完整的 bash 解析器。
"""

import posixpath
import re
import shlex

# <<[-]DELIM 之后同一行的内容(rest,例如 `> out.txt`)仍是命令的一部分,只有正文(body)是数据
HEREDOC = re.compile(
    r"(?<!<)<<(?!<)-?\s*(['\"]?)(\w+)\1(?P<rest>[^\n]*)\n(?P<body>.*?)^\s*\2\s*$", re.S | re.M
)
ENV_ASSIGN = re.compile(r"^\w+=")
SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
# 正文只是写进文件的数据:命令检查和路径检查都不该看它
DATA_HEREDOC_HEADS = frozenset({"cat", "tee"})
_OPERATORS = {"&&", "||", ";", "|", "&", "\n", ";;", "(", ")"}


def quoted_at(text: str, pos: int) -> bool:
    """text[pos] 是否落在引号内(简化扫描:处理 \\ 转义与单双引号)。"""
    quote = ""
    i = 0
    while i < pos:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = ""
        elif ch == "\\":
            i += 1
        elif quote == '"':
            if ch == '"':
                quote = ""
        elif ch in "'\"":
            quote = ch
        i += 1
    return bool(quote)


def _head_before(command: str, pos: int) -> str:
    """pos(heredoc 的 <<)所在命令的命令名;认不出返回空串。"""
    line = command[command.rfind("\n", 0, pos) + 1 : pos]
    try:
        words = shlex.split(re.split(r"[;&|(]", line)[-1])
    except ValueError:
        return ""
    while words and ENV_ASSIGN.match(words[0]):
        words = words[1:]
    return posixpath.basename(words[0]) if words else ""


def heredocs(command: str) -> list[tuple[re.Match, str]]:
    """未被引号包住的 heredoc 及其所属命令名。引号里的 <<X 只是普通文本,不能当 heredoc,
    否则会吞掉后面真正的命令。"""
    found = []
    for m in HEREDOC.finditer(command):
        if not quoted_at(command, m.start()):
            found.append((m, _head_before(command, m.start())))
    return found


def strip_heredocs(command: str, only: frozenset[str] | None = None) -> str:
    """剔除 heredoc 正文(保留 `<<X` 同一行的其余部分,如 `> out.txt`)。

    默认剔除所有正文,但 `bash <<EOF` 这类正文本身会被当 shell 执行,保留。
    only 非空时只剔除这些命令的 heredoc:`cat > a.html <<EOF` 的正文是数据,
    而 `python3 - <<EOF` 的正文是代码,路径检查仍要看它。
    """
    out: list[str] = []
    last = 0
    for m, head in heredocs(command):
        if (head not in only) if only is not None else (head in SHELLS):
            continue
        out.append(command[last : m.start()])
        out.append(m.group("rest") + "\n")
        last = m.end()
    out.append(command[last:])
    return "".join(out)


def split_commands(command: str) -> list[list[str]] | None:
    """按 shell 语法切成若干条命令(词列表)。heredoc 正文只是数据,先剔除;引号里的 ; && 不会被误切。

    解析不了返回 None。
    """
    lexer = shlex.shlex(strip_heredocs(command), posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token in _OPERATORS or (set(token) <= set("&|;()\n") and token):
            commands.append([])
        else:
            commands[-1].append(token)
    return [c for c in commands if c]
