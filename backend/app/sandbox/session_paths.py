"""Path helpers for a single sandbox session directory tree.

``sandbox_data`` is bind-mounted into the container (``./sandbox_data`` ->
``/sandbox_data``), so the host tree and the container tree are the *same*
files and differ only by prefix. Container paths are therefore derived from
host paths via :func:`host_to_container` instead of being tracked separately.
"""

import os
import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.sandbox.constants import (
    CONTAINER_DEFAULT_SKILLS,
    CONTAINER_SANDBOX_DATA_ROOT,
    DEFAULT_SKILLS_DIR,
    SANDBOX_DATA_ROOT,
    SESSION_ROOT,
    SESSION_SUBDIRS,
    VIRTUAL_WORKSPACE,
)
from app.sandbox.host_files import parent_fd


def normalize_virtual_root(virtual_root: str) -> str:
    """Ensure virtual root is an absolute POSIX path (e.g. /workspace)."""
    root = virtual_root.rstrip("/")
    if not root.startswith("/"):
        root = f"/{root}"
    return root


def host_to_container(host_path: Path) -> str:
    """Map a host path under SANDBOX_DATA_ROOT to its container path.

    The bind mount guarantees host ``SANDBOX_DATA_ROOT`` == container
    ``/sandbox_data``, so this is a pure prefix swap.

    Example:
    project_root/sandbox_data/sessions/123 -> /sandbox_data/sessions/123
    """
    rel = host_path.resolve().relative_to(SANDBOX_DATA_ROOT.resolve())
    if not rel.parts:
        return CONTAINER_SANDBOX_DATA_ROOT
    return posixpath.join(CONTAINER_SANDBOX_DATA_ROOT, *rel.parts)


@dataclass(frozen=True)
class SessionPaths:
    """Immutable view of one session's on-disk and virtual layout.

    Host:      sandbox_data/sessions/<session_id>/{uploads,output,scratchpad}
    Container: /sandbox_data/sessions/<session_id>/...
    Virtual:   /workspace/{uploads,output,scratchpad,skills}
    """

    session_id: str  # session_id is the id of the session
    session_root: Path  # session_root is the root of the session directory on the host
    virtual_root: str  # virtual_root AKA workspace/

    @classmethod
    def create(
        cls,
        session_id: str,
        sessions_root: Path | None = None,
        virtual_root: str = VIRTUAL_WORKSPACE,
    ) -> "SessionPaths":
        from app.schemas import validate_session_id

        validate_session_id(session_id)
        root = (sessions_root or SESSION_ROOT).resolve()
        return cls(
            session_id=session_id,
            session_root=root / session_id,
            virtual_root=normalize_virtual_root(virtual_root),
        )

    # Container root is derived from the host root via the bind-mount prefix.
    @property
    def container_root(self) -> str:
        return host_to_container(self.session_root)

    @property
    def uploads(self) -> Path:
        return self.session_root / "uploads"

    @property
    def output(self) -> Path:
        return self.session_root / "output"

    @property
    def scratchpad(self) -> Path:
        return self.session_root / "scratchpad"

    @property
    def subdirs(self) -> tuple[Path, ...]:
        return tuple(self.session_root / name for name in SESSION_SUBDIRS)

    @property
    def virtual_scratchpad(self) -> str:
        return f"{self.virtual_root}/scratchpad"

    @property
    def virtual_skills(self) -> str:
        return f"{self.virtual_root}/skills"

    def virtual_subdir(self, name: str) -> str:
        return f"{self.virtual_root}/{name}"

    def virtual_uploads(self) -> str:
        return f"{self.virtual_root}/uploads"

    def resolve_host_path(self, relative_path: str) -> Path:
        """Resolve a path relative to session root; raises ValueError on escape.

        Example:
        "uploads/foo.txt" -> project_root/sandbox_data/sessions/123/uploads/foo.txt
        """
        candidate = (self.session_root / relative_path).resolve()
        session_root = self.session_root.resolve()
        try:
            candidate.relative_to(session_root)
        except ValueError as exc:
            raise ValueError(f"Path '{relative_path}' escapes session directory") from exc
        return candidate

    def virtual_to_host_path(self, virtual_path: str) -> Path:
        """Map a sandbox path like /workspace/uploads/foo to a host Path.

        Example:
        /workspace/skills/foo.py -> project_root/sandbox_data/default_skills/foo.py
        /workspace/uploads/foo.txt -> project_root/sandbox_data/sessions/123/uploads/foo.txt
        """
        is_skills, rel = self._resolve_virtual(virtual_path)
        if is_skills:
            if not DEFAULT_SKILLS_DIR.is_dir():
                raise ValueError("Skills directory is not available in this sandbox")
            return self._resolve_global_path(DEFAULT_SKILLS_DIR, rel)
        return self.resolve_host_path(rel)

    def virtual_to_container_path(self, virtual_path: str) -> str:
        """Map /workspace/... to the physical container path for docker exec.

        Example:
        /workspace/skills/foo.py -> /sandbox_data/default_skills/foo.py
        /workspace/uploads/foo.txt -> /sandbox_data/sessions/123/uploads/foo.txt
        """
        is_skills, rel = self._resolve_virtual(virtual_path)
        if is_skills:
            return f"{CONTAINER_DEFAULT_SKILLS}/{rel}" if rel else CONTAINER_DEFAULT_SKILLS
        return f"{self.container_root}/{rel}" if rel else self.container_root

    def container_to_relative(self, container_path: str) -> str:
        """Map a physical container path to its workspace-relative path.

        Example:
        /sandbox_data/default_skills/foo.py -> skills/foo.py
        /sandbox_data/sessions/123/uploads/foo.txt -> uploads/foo.txt
        /sandbox_data/sessions/123 -> ""
        """
        normalized = posixpath.normpath(container_path.rstrip("/"))
        skills_root = CONTAINER_DEFAULT_SKILLS.rstrip("/")
        if normalized == skills_root or normalized.startswith(f"{skills_root}/"):
            rel = normalized[len(skills_root) :].lstrip("/")
            return f"skills/{rel}" if rel else "skills"

        root = self.container_root.rstrip("/")
        if normalized == root:
            return ""
        prefix = f"{root}/"
        if not normalized.startswith(prefix):
            raise ValueError(f"Path '{container_path}' is outside session directory")
        return normalized[len(prefix) :]

    def container_to_virtual_path(self, container_path: str) -> str:
        """Map a physical container path back to the agent-facing virtual path.

        Example:
        /sandbox_data/default_skills/foo.py -> /workspace/skills/foo.py
        /sandbox_data/sessions/123/uploads/foo.txt -> /workspace/uploads/foo.txt
        """
        rel = self.container_to_relative(container_path)
        return f"{self.virtual_root}/{rel}" if rel else self.virtual_root

    def _resolve_virtual(self, virtual_path: str) -> tuple[bool, str]:
        """Split a /workspace path into (is_skills, relative).

        Example:
        /workspace/skills/foo.py -> (True, "foo.py")
        /workspace/uploads/foo.txt -> (False, "uploads/foo.txt")
        """
        rel = self._relative_under_virtual(virtual_path)
        if rel == "skills" or rel.startswith("skills/"):
            return True, rel[len("skills") :].lstrip("/")
        return False, rel

    def _relative_under_virtual(self, virtual_path: str) -> str:
        """Ensure a path is relative to the workspace root.

        Example:
        /workspace/skills/foo.py -> skills/foo.py
        /workspace/uploads/foo.txt -> uploads/foo.txt
        """
        workspace = self.virtual_root
        normalized = posixpath.normpath(virtual_path.rstrip("/"))
        if normalized != workspace and not normalized.startswith(f"{workspace}/"):
            raise ValueError(f"Path '{virtual_path}' is outside virtual workspace ({workspace})")
        return normalized[len(workspace) :].lstrip("/")

    def read_host_file(self, host_path: Path, max_bytes: int | None = None) -> bytes:
        """读会话目录里 agent 可写的文件,不跟随符号链接。

        沙箱里的命令能把产物换成指向后端自身文件(如 /proc/1/environ、data/ 下的数据库)的链接,
        直接 read_text 会把后端的内容读出来交给模型。末段用 O_NOFOLLOW 拒绝链接,
        中间各段通过目录 fd 打开,拒绝路径解析后被替换的符号链接。
        """
        with self.open_host_file(host_path) as fh:
            return fh.read() if max_bytes is None else fh.read(max_bytes)

    def open_host_file(self, host_path: Path) -> BinaryIO:
        """read_host_file 的流式版本:同样的检查,返回已打开的二进制文件对象。"""
        root = self.session_root.resolve()
        parent = host_path.parent.resolve()
        if parent != root and not parent.is_relative_to(root):
            raise ValueError(f"Path '{host_path}' escapes session directory")
        # Do not use the newly resolved parent to open the file: a replaced
        # directory could redirect a previously validated output path elsewhere.
        with parent_fd(root, host_path) as (directory, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError(f"Not a regular file: {host_path.name}")
        return os.fdopen(fd, "rb")

    @staticmethod
    def _resolve_global_path(root: Path, relative_path: str) -> Path:
        candidate = (root / relative_path).resolve()
        root_resolved = root.resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError as exc:
            raise ValueError(f"Path '{relative_path}' escapes global directory") from exc
        return candidate
