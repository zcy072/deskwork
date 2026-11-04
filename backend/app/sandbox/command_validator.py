"""Bash 命令的预检查。

这是尽力而为的纵深防御层(advisory),用来给 agent 友好的报错、挡住明显的误操作;
它不是安全边界,也不可能完整解析 bash(变量展开、eval、base64 解码执行等都能绕过)。
真正的隔离边界是 bwrap(只读根、独立 PID / mount 命名空间、仅绑定会话目录)
以及沙箱容器本身的 seccomp / cap_drop / 无网络 / 只读根文件系统。
"""

import posixpath
import re
import shlex
from pathlib import Path

from app.sandbox.constants import (
    BLOCKED_COMMAND_PATTERNS,
    BLOCKED_COMMANDS,
    VIRTUAL_SCRATCHPAD,
    VIRTUAL_WORKSPACE,
    WORKSPACE_TOP_LEVELS,
)
from app.sandbox.shell_parse import (
    DATA_HEREDOC_HEADS,
    ENV_ASSIGN,
    SHELLS,
    split_commands,
    strip_heredocs,
)
from app.utils import logger

_MAX_DEPTH = 4
_KEYWORDS = frozenset(
    {
        "{",
        "}",
        "!",
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "do",
        "done",
        "while",
        "until",
        "for",
        "case",
        "esac",
    }
)
_SUBSTITUTION_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
# 这些命令的参数里还有一条完整命令,或者会把后面的词当命令执行
_WRAPPERS = frozenset(
    {"sudo", "env", "nohup", "time", "command", "exec", "nice", "timeout", "xargs", "builtin"}
)


class CommandValidator:
    """Validates bash commands before they are sent to a session shell."""

    _ABSOLUTE_PATH_RE = re.compile(r"/(?:[\w.\-]+/)*[\w.\-]+")
    _STRING_LITERAL_RE = re.compile(r"""['"]([^'"]+)['"]""")

    def validate(
        self,
        command: str,
        current_cwd: str | None = None,
        virtual_root: str = VIRTUAL_WORKSPACE,
    ) -> tuple[bool, str | None]:
        command = command.strip()
        if not command:
            return False, "Empty command"

        cwd = current_cwd or VIRTUAL_SCRATCHPAD

        blocked_reason = self._check_blocked_commands(command)
        if blocked_reason:
            return False, blocked_reason

        # 写进文件的 heredoc 正文是数据(如文档里贴的 `curl ... | bash` 示例),不按命令模式拦
        pattern_reason = self._check_blocked_patterns(strip_heredocs(command, DATA_HEREDOC_HEADS))
        if pattern_reason:
            return False, pattern_reason

        path_reason = self._check_paths(command, cwd, virtual_root)
        if path_reason:
            return False, path_reason

        cd_reason = self._check_cd(command, cwd, virtual_root)
        if cd_reason:
            return False, cd_reason

        return True, None

    @staticmethod
    def normalize_session_virtual_path(
        raw_path: str,
        virtual_root: str = VIRTUAL_WORKSPACE,
    ) -> str:
        """Map agent-facing shorthand like /uploads/foo to /workspace/uploads/foo."""
        if not raw_path.startswith("/"):
            top_level = raw_path.split("/", 1)[0]
            if top_level in WORKSPACE_TOP_LEVELS:
                return posixpath.normpath(f"{virtual_root.rstrip('/')}/{raw_path}")
            return raw_path

        stripped = raw_path.lstrip("/")
        if not stripped:
            return raw_path

        top_level, _, _remainder = stripped.partition("/")
        root = virtual_root.rstrip("/")
        if top_level == root.lstrip("/"):
            return posixpath.normpath(raw_path)
        if top_level in WORKSPACE_TOP_LEVELS:
            return posixpath.normpath(f"{root}/{stripped}")
        return posixpath.normpath(raw_path)

    @staticmethod
    def resolve_virtual_path(raw_path: str, cwd: str) -> str:
        """Resolve a path against virtual cwd; return normalized virtual path."""
        normalized = CommandValidator.normalize_session_virtual_path(raw_path)
        if normalized.startswith("/"):
            return posixpath.normpath(normalized)
        return posixpath.normpath(posixpath.join(cwd, normalized))

    @staticmethod
    def within_workspace(resolved: str, virtual_root: str = VIRTUAL_WORKSPACE) -> bool:
        root = virtual_root.rstrip("/")
        normalized = posixpath.normpath(resolved).rstrip("/")
        return normalized == root or normalized.startswith(f"{root}/")

    @staticmethod
    def resolve_container_path(
        raw_path: str,
        cwd: str,
        virtual_root: str = VIRTUAL_WORKSPACE,
    ) -> tuple[bool, str]:
        """Resolve a path against cwd; return (within_workspace, resolved_virtual)."""
        resolved = CommandValidator.resolve_virtual_path(raw_path, cwd)
        return CommandValidator.within_workspace(resolved, virtual_root), resolved

    def _check_blocked_commands(self, command: str, depth: int = 0) -> str | None:
        """只看命令位置的词:`git init`、`grep exit f`、heredoc 正文里的 exit 不是被禁的命令。
        bash -c / eval 的字符串参数和命令替换里的命令也会执行,递归检查。"""
        commands = split_commands(command)
        if commands is None:
            return "Could not parse command"
        if not commands:
            return "Empty command"

        for words in commands:
            reason = self._blocked_in_words(words, depth)
            if reason:
                return reason
        # 命令替换(反引号和 $(...))可能藏在引号里,按文本取出来单独检查
        if depth < _MAX_DEPTH:
            for inner in _SUBSTITUTION_RE.findall(command):
                reason = self._check_blocked_commands(inner[0] or inner[1], depth + 1)
                if reason and reason != "Could not parse command":
                    return reason
        return None

    def _blocked_in_words(self, words: list[str], depth: int) -> str | None:
        # 变量赋值和 shell 关键字(`{ kill 1; }`、`if x; then kill 1`、`! kill 1`)之后才是真正的命令
        while words and (ENV_ASSIGN.match(words[0]) or words[0] in _KEYWORDS):
            words = words[1:]
        if not words:
            return None
        base = Path(words[0]).name
        if base in BLOCKED_COMMANDS:
            return f"Command '{base}' is blocked"
        if depth >= _MAX_DEPTH:
            return None
        if base in SHELLS:
            for i, arg in enumerate(words[1:-1], 1):
                if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
                    return self._check_blocked_commands(words[i + 1], depth + 1)
        elif base == "eval":
            return self._check_blocked_commands(" ".join(words[1:]), depth + 1)
        elif base in _WRAPPERS:
            # 包装命令后面哪个词是真命令不好判断,逐个当命令头试
            for i in range(1, len(words)):
                reason = self._blocked_in_words(words[i:], depth + 1)
                if reason:
                    return reason
        return None

    def _check_blocked_patterns(self, command: str) -> str | None:
        for pattern, reason in BLOCKED_COMMAND_PATTERNS:
            if re.search(pattern, command):
                logger.warning(f"Command matches blocked pattern: {pattern}")
                return reason
        return None

    def _extract_absolute_paths(self, command: str) -> list[str]:
        token_start_chars = set(" \t\n;|&=:(,")
        paths: list[str] = []
        for match in self._ABSOLUTE_PATH_RE.finditer(command):
            start = match.start()
            if start > 0:
                prev = command[start - 1]
                if prev == "." or prev not in token_start_chars:
                    continue
            paths.append(match.group())
        return paths

    @staticmethod
    def _extract_relative_escapes(command: str) -> list[str]:
        """未加引号的相对路径(如 ``cat ../../etc/passwd``、``--out=../x``)里含 ``..`` 的片段。

        只挑含 ``..`` 路径段的,其余相对路径不可能离开 cwd,无需检查。
        """
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            return []
        found: list[str] = []
        for token in tokens:
            for part in re.split(r"[=:,]", token):
                if not part.startswith("/") and ".." in part.split("/"):
                    found.append(part)
        return found

    def _check_paths(
        self,
        command: str,
        current_cwd: str,
        virtual_root: str,
    ) -> str | None:
        paths_to_check: list[str] = []
        # cat/tee 的 heredoc 正文只是写进文件的数据(HTML 里的 href="/docs" 不是路径)
        command = strip_heredocs(command, DATA_HEREDOC_HEADS)

        for match in self._extract_absolute_paths(command):
            paths_to_check.append(match)

        for match in self._STRING_LITERAL_RE.findall(command):
            if match.startswith("/"):
                paths_to_check.append(match)
            elif "/" in match or match.startswith("."):
                paths_to_check.append(match)

        paths_to_check.extend(self._extract_relative_escapes(command))

        for raw_path in paths_to_check:
            reason = self._validate_single_path(raw_path, current_cwd, virtual_root)
            if reason:
                return reason

        return None

    def _check_cd(
        self,
        command: str,
        current_cwd: str,
        virtual_root: str,
    ) -> str | None:
        for segment in re.split(r"\s*;\s*|\s*&&?\s*|\s*\|\|?\s*|\n", command):
            segment = segment.strip().lstrip("({! \t")
            if not segment:
                continue
            try:
                tokens = shlex.split(segment)
            except ValueError:
                continue
            # `builtin cd x` / `command cd x` 与 `cd x` 等价
            while tokens and tokens[0] in ("builtin", "command"):
                tokens = tokens[1:]
            if not tokens:
                continue
            head = tokens[0]
            if head == "eval":
                reason = self._check_cd(" ".join(tokens[1:]), current_cwd, virtual_root)
                if reason:
                    return reason
                continue
            if head in ("source", "."):
                if len(tokens) > 1:
                    reason = self._validate_single_path(tokens[1], current_cwd, virtual_root)
                    if reason:
                        return reason
                continue
            if head not in ("cd", "pushd"):
                continue
            if len(tokens) == 1:
                return f"{head} without argument is not allowed in session shell"
            reason = self._validate_cd_target(tokens[1], current_cwd, virtual_root)
            if reason:
                return reason
        return None

    def _validate_cd_target(
        self,
        raw_path: str,
        current_cwd: str,
        virtual_root: str,
    ) -> str | None:
        within, _ = self.resolve_container_path(raw_path, current_cwd, virtual_root)
        if not within:
            return f"cd to '{raw_path}' escapes workspace directory"
        return None

    def _validate_single_path(
        self,
        raw_path: str,
        current_cwd: str,
        virtual_root: str,
    ) -> str | None:
        if raw_path.startswith("/"):
            effective = self.normalize_session_virtual_path(raw_path, virtual_root)
            if self.within_workspace(effective, virtual_root):
                return None
            return f"Path '{raw_path}' is outside workspace directory"

        resolved = self.resolve_virtual_path(raw_path, current_cwd)
        if not self.within_workspace(resolved, virtual_root):
            return f"Path '{raw_path}' escapes workspace directory"
        return None
