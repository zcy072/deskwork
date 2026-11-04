import posixpath
import re

from app.sandbox.shell_parse import ENV_ASSIGN, SHELLS, split_commands, strip_heredocs

SCRATCH = "/workspace/scratchpad"

_REASON_DELETE = "删除文件或目录"
_REASON_WIPE = "清空或销毁文件内容"
_REASON_DD = "用 dd 直接写入目标"
_REASON_FIND = "批量删除文件"
_REASON_MV = "移动用户上传的文件"
_REASON_TEE = "用 tee 覆盖用户上传的文件"
_REASON_CHMOD = "递归修改权限"
_REASON_UNINSTALL = "卸载依赖"
_REASON_REDIRECT = "覆盖用户上传的文件"

# 通过安全校验、但不可逆或影响面大的命令:执行前需要用户确认。(正则, 原因)
RISKY_COMMAND_PATTERNS = (
    (r"\brm\b|\brmdir\b|\bunlink\b", _REASON_DELETE),
    (r"\bshred\b|\btruncate\b", _REASON_WIPE),
    (r"\bdd\b[^|;&]*\bof=", _REASON_DD),
    (r"\bfind\b[^|;&]*(-delete|-exec\s+rm)", _REASON_FIND),
    (r"\bmv\b[^|;&]*\buploads/", _REASON_MV),
    (r">\s*(\./|/workspace/)?uploads/", _REASON_REDIRECT),
    (r"\btee\b[^|;&]*\buploads/", _REASON_TEE),
    (r"\bchmod\s+-R\b|\bchown\s+-R\b", _REASON_CHMOD),
    (r"\bpip\s+uninstall\b|\bnpm\s+uninstall\b", _REASON_UNINSTALL),
)


def _path_in_scratch(path: str, in_scratch_cwd: bool) -> bool:
    if ".." in path.split("/") or "$" in path or "`" in path or path.startswith("~"):
        return False
    if path.startswith("/"):
        return path == SCRATCH or path.startswith(SCRATCH + "/")
    return in_scratch_cwd


_DELETERS = ("rm", "rmdir", "unlink")


def _rm_only_in_scratch(command: str) -> bool:
    """命令里每个 rm/rmdir/unlink 的目标都明确位于 scratchpad(模型清理自己的临时文件),才视为安全。

    相对路径只有在同一条命令里先 cd 到 scratchpad 时才认;拿不准一律按风险处理。
    """
    commands = split_commands(command)
    if commands is None:
        return False
    in_scratch_cwd = False
    for words in commands:
        if posixpath.basename(words[0]) == "ln":
            # 同一条命令里先建链接再删:scratchpad 里的链接可以指向 output/,路径判断就失效了
            return False
        if words[0] == "cd":
            # 只有 `cd <scratchpad 内绝对路径>` 才算仍在 scratchpad;裸 cd(回家目录)、cd ~、cd - 等一律不算
            in_scratch_cwd = (
                len(words) == 2
                and not words[1].startswith("~")
                and _path_in_scratch(words[1], False)
            )
            continue
        if words[0] not in _DELETERS:
            if any(re.search(r"\b(rm|rmdir|unlink)\b", w) for w in words):
                return False  # rm 藏在 bash -c、引号等位置,无法判断
            continue
        targets = [w for w in words[1:] if not w.startswith("-")]
        if not targets or not all(_path_in_scratch(t, in_scratch_cwd) for t in targets):
            return False
    return True


# 这些命令的参数里还有一条完整命令,或者会把后面的词当命令执行
_WRAPPERS = {
    "sudo",
    "env",
    "nohup",
    "time",
    "command",
    "exec",
    "nice",
    "timeout",
    "xargs",
    "builtin",
}
_REDIRECT_UPLOADS = re.compile(r">\s*(\./|/workspace/)?uploads/")


def _word_reasons(words: list[str]) -> set[str]:
    """单条命令(已按 ; && | 切开)的风险原因。只看命令位置的词,文件名或字符串里的 rm、dd 不算。"""
    while words and ENV_ASSIGN.match(words[0]):
        words = words[1:]
    if not words:
        return set()
    reasons: set[str] = set()
    for i, word in enumerate(words[:-1]):  # python -m pip uninstall 之类
        if posixpath.basename(word) in ("pip", "pip3", "npm") and words[i + 1] == "uninstall":
            reasons.add(_REASON_UNINSTALL)
    return reasons | _head_reasons(words)


def _head_reasons(words: list[str]) -> set[str]:
    """按命令头(words[0])判断风险;包装命令则把后面每个词都当命令头试一遍。"""
    head, args = posixpath.basename(words[0]), words[1:]
    reasons: set[str] = set()

    if head in _DELETERS:
        reasons.add(_REASON_DELETE)
    elif head in ("shred", "truncate"):
        reasons.add(_REASON_WIPE)
    elif head == "dd":
        if any(a.startswith("of=") for a in args):
            reasons.add(_REASON_DD)
    elif head == "find":
        if "-delete" in args:
            reasons.add(_REASON_FIND)
        for i, a in enumerate(args[:-1]):
            if a in ("-exec", "-execdir", "-ok") and posixpath.basename(args[i + 1]) in (
                *_DELETERS,
                "shred",
            ):
                reasons.add(_REASON_FIND)
    elif head == "mv":
        if any("uploads/" in a for a in args):
            reasons.add(_REASON_MV)
    elif head == "tee":
        if any("uploads/" in a for a in args):
            reasons.add(_REASON_TEE)
    elif head in ("chmod", "chown"):
        if any(
            a == "--recursive" or (a.startswith("-") and not a.startswith("--") and "R" in a)
            for a in args
        ):
            reasons.add(_REASON_CHMOD)
    elif head in SHELLS:
        for i, a in enumerate(args[:-1]):
            if a.startswith("-") and not a.startswith("--") and "c" in a:
                reasons |= _scan(args[i + 1])
                break
    elif head == "eval":
        reasons |= _scan(" ".join(args))
    elif head in _WRAPPERS:
        # 包装命令后面哪个词是真命令不好判断,逐个当命令头试。这里已经覆盖了所有后缀,
        # 遇到嵌套的包装命令就不必再递归,否则 `nice nice nice ... ls` 会指数级变慢。
        for i in range(1, len(words)):
            if posixpath.basename(words[i]) not in _WRAPPERS:
                reasons |= _head_reasons(words[i:])
    return reasons


def _legacy_reasons(text: str) -> set[str]:
    return {reason for pattern, reason in RISKY_COMMAND_PATTERNS if re.search(pattern, text)}


def _scan(command: str) -> set[str]:
    commands = split_commands(command)
    if commands is None:  # 解析不了:退回对整条字符串的正则匹配,宁可多问
        return _legacy_reasons(command)
    reasons: set[str] = set()
    for words in commands:
        reasons |= _word_reasons(words)
        for w in words:
            if "$(" in w or "`" in w:  # 引号里的命令替换也会执行
                reasons |= _legacy_reasons(w)
    if _REDIRECT_UPLOADS.search(strip_heredocs(command)):
        reasons.add(_REASON_REDIRECT)
    return reasons


def risky_reason(command: str) -> str | None:
    reasons = _scan(command)
    for _pattern, reason in RISKY_COMMAND_PATTERNS:  # 按原顺序返回第一个
        if reason not in reasons:
            continue
        if reason == _REASON_DELETE and _rm_only_in_scratch(command):
            continue
        return reason
    return None
