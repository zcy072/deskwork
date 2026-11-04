import asyncio
import hashlib
import os
import secrets
import shlex
import shutil
import stat
import time
from asyncio import StreamReader
from contextlib import asynccontextmanager
from pathlib import Path

from app.config import settings
from app.sandbox.command_validator import CommandValidator
from app.sandbox.constants import (
    BASH_SENTINEL,
    COMMAND_TIMEOUT_SEC,
    CONTAINER_AGENT_USER,
    CONTAINER_DEFAULT_SKILLS,
    CONTAINER_NAME,
    OUTPUT_MAX_LINES,
    SESSION_ROOT,
    SESSION_SUBDIRS,
    SPILL_MAX_CHARS,
    WRITABLE_SESSION_SUBDIRS,
)
from app.sandbox.errors import ContainerFileSystemError
from app.sandbox.manager_client import ManagerClient, RemoteProcess, _cap_timeout
from app.sandbox.quota import (
    MB,
    SNAPSHOT_EXCLUDED,
    is_cleanup_command,
    over_quota_message,
    session_usage,
    snapshot_manifest,
    snapshot_name,
)
from app.sandbox.session_paths import SessionPaths
from app.utils import logger

# 上一条命令结束时测得的会话占用在这么多秒内直接复用,连续执行命令时不必每条都遍历两次目录
USAGE_CACHE_SEC = 10.0

# 文件工具(view_file、str_replace 等)一次读取的上限;更大的文件用 bash 查看
MAX_READ_BYTES = 50 * 1024 * 1024

# 单条命令累计输出上限(字节),超过即杀掉 shell,防止 `yes` 之类的命令撑爆后端内存
MAX_COMMAND_OUTPUT_BYTES = 20 * 1024 * 1024


class OutputLimitExceeded(Exception):
    """单条命令的输出超过 MAX_COMMAND_OUTPUT_BYTES。"""


class ShellExited(Exception):
    """命令执行期间 shell 自己退出了(set -e 后有命令失败、${X:?}、语法错误等)。"""


class BashSession:
    """Stateful, isolated bash session for a single agent session."""

    def __init__(
        self,
        paths: SessionPaths,
        container_name: str = CONTAINER_NAME,
        validator: CommandValidator | None = None,
        docker_client: ManagerClient | None = None,
        exec_user: str = CONTAINER_AGENT_USER,
    ) -> None:
        self.paths = paths
        self.container_name = container_name
        self.validator = validator or CommandValidator()
        self.docker_client = docker_client or ManagerClient()
        self.exec_user = exec_user
        self._lock = asyncio.Lock()
        self._process: RemoteProcess | None = None
        # (label, 行);行为 None 表示该流的读取已结束(EOF 或超出输出上限),用来唤醒等待方
        self._output_queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        self._reader_tasks: list[asyncio.Task] = []
        self._active_exec_user = exec_user
        self._current_cwd = paths.virtual_scratchpad
        self._spill_count = 0
        self._output_bytes = 0
        self._output_exceeded = False
        # 本条命令的结束标记:带随机 nonce,命令输出里出现固定的 __BASH_DONE__ 不会被当成结束
        self._sentinel = BASH_SENTINEL
        self.last_used = time.monotonic()
        self._usage: tuple[float, int] | None = None  # (测量时刻, 字节数)

    def validate_command(self, command: str) -> tuple[bool, str | None]:
        return self.validator.validate(
            command,
            current_cwd=self._current_cwd,
            virtual_root=self.paths.virtual_root,
        )

    async def execute(
        self, command: str, timeout: int | None = COMMAND_TIMEOUT_SEC
    ) -> tuple[str, bool]:
        timeout_sec = _cap_timeout(timeout)
        started = time.monotonic()
        command_digest = hashlib.sha256(command.encode()).hexdigest()[:12]

        async with self._lock, self._cleanup_on_cancel():
            self.last_used = time.monotonic()
            # 校验依赖当前 cwd,必须在拿到锁之后:排队期间前一条命令可能已经 cd 走了
            ok, reason = self.validate_command(command)
            if not ok:
                return f"Error: {reason}", True
            quota_limit = settings.SESSION_MAX_STORAGE_MB
            if quota_limit:
                used = await self._session_usage(reuse=True)
                if used > quota_limit * MB and not is_cleanup_command(command):
                    return f"Error: {over_quota_message(used, quota_limit)}", True
            await self._ensure_started(self.exec_user)
            await self._drain_queue()
            self._output_bytes = 0
            self._output_exceeded = False
            wrapped = self._wrap_command(command)
            await self.docker_client.write_stdin(self._process, wrapped)
            try:
                output, timed_out, cwd = await self._read_until_sentinel(timeout_sec)
            except ShellExited as exc:
                logger.warning(
                    "sandbox shell exited: session=%s command=%s reason=%s",
                    self.paths.session_id,
                    command_digest,
                    type(exc).__name__,
                )
                await self._stop()
                await self._ensure_started(self._active_exec_user)
                code = f" (exit code {exc})" if str(exc) not in ("", "None") else ""
                return (
                    f"Error: The shell exited while running the command{code}; "
                    "it was restarted, so the working directory and variables were reset. "
                    "Avoid `set -e` / `set -u` in the session shell; wrap such scripts in "
                    "`bash -c '...'` or a script file.",
                    True,
                )
            except OutputLimitExceeded:
                logger.warning(
                    "sandbox command output limit exceeded: session=%s command=%s",
                    self.paths.session_id,
                    command_digest,
                )
                await self._stop()
                await self._ensure_started(self._active_exec_user)
                limit_mb = MAX_COMMAND_OUTPUT_BYTES // MB
                return (
                    f"Error: Output limit exceeded ({limit_mb}MB); the command was killed "
                    "and the shell restarted. Redirect output to a file and read parts of it.",
                    True,
                )
            if timed_out:
                logger.warning(
                    "sandbox command timed out: session=%s command=%s timeout_sec=%s",
                    self.paths.session_id,
                    command_digest,
                    timeout_sec,
                )
                return f"Error: Command timed out after {timeout_sec} seconds", True

            if cwd is not None and not self._cwd_within_session(cwd):
                logger.warning(
                    "Session %s cwd escaped boundary: %s",
                    self.paths.session_id,
                    cwd,
                )
                await self._stop()
                await self._ensure_started(self._active_exec_user)
                return (
                    f"Error: Working directory escaped session boundary ({cwd})",
                    True,
                )

            if cwd is not None:
                self._current_cwd = cwd

            logger.info(
                "sandbox command completed: session=%s command=%s elapsed_ms=%d output_chars=%d",
                self.paths.session_id,
                command_digest,
                int((time.monotonic() - started) * 1000),
                len(output),
            )
            if quota_limit:
                used = await self._session_usage(reuse=False)
                if used > quota_limit * MB:
                    output += f"\n\nWarning: {over_quota_message(used, quota_limit)}"
            return output, False

    @asynccontextmanager
    async def _cleanup_on_cancel(self):
        # 必须在持锁期间覆盖启动/初始化/发送阶段,而不只覆盖等待命令输出。
        # 等锁的另一条命令被取消时尚未进入这里,不会误杀正在执行的命令。
        try:
            yield
        except asyncio.CancelledError:
            logger.info("Session %s cancelled, killing sandbox shell", self.paths.session_id)
            await asyncio.shield(self._stop())
            raise

    async def _session_usage(self, *, reuse: bool) -> int:
        """会话占用。reuse=True 时复用 USAGE_CACHE_SEC 内的上次测量结果。

        上传会自己检查配额;create_file 等工具写的内容在下一条命令结束时计入,
        缓存窗口内可能暂时没算上。配额本来就是尽力而为的软限制。
        """
        now = time.monotonic()
        if reuse and self._usage and now - self._usage[0] < USAGE_CACHE_SEC:
            return self._usage[1]
        used = await asyncio.to_thread(session_usage, self.paths.session_root)
        self._usage = (now, used)
        return used

    async def reset(self, user: str | None = None) -> str:
        async with self._lock, self._cleanup_on_cancel():
            await self._stop()
            self._current_cwd = self.paths.virtual_scratchpad
            await self._ensure_started(user or self.exec_user)
            return "Bash session restarted"

    async def save_upload(self, dest_name: str, data: bytes) -> Path:
        """把上传内容直接写进 uploads(调用方已保证文件名唯一)。

        通过 bind mount 立即在容器内可见;agent 经由 uploads 目录的 r-x 权限读取。
        """
        if ".." in Path(dest_name).parts or Path(dest_name).is_absolute():
            raise ValueError("Invalid destination filename")
        host_dest = self.paths.uploads / dest_name
        await asyncio.to_thread(self._create_upload, host_dest, data)
        return host_dest

    @staticmethod
    def _create_upload(host_dest: Path, data: bytes) -> None:
        host_dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(host_dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o665)
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fd, 0o665)  # 不受 umask 影响
            fh.write(data)

    async def close(self) -> None:
        async with self._lock:
            await self._stop()

    @property
    def shell_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def close_if_idle(self) -> bool:
        """没有命令在跑时关掉 shell(下次 execute 会自动重启);正忙则跳过。"""
        if self._lock.locked() or not self.shell_alive:
            return False
        # 锁空闲时 acquire 不会让出事件循环,检查和加锁之间不会插进别的命令
        async with self._lock:
            await self._stop()
        return True

    # ── Container filesystem operations (raw docker exec as agent user) ──

    def resolve_virtual_path(self, raw_path: str) -> str:
        """Resolve an agent path to a virtual /workspace/... path."""
        within, resolved = CommandValidator.resolve_container_path(
            raw_path,
            self._current_cwd,
            self.paths.virtual_root,
        )
        if not within:
            raise ContainerFileSystemError(f"Path '{raw_path}' escapes workspace directory")
        return resolved

    def resolve_container_path(self, raw_path: str) -> str:
        """Resolve an agent path to the physical container path for docker exec."""
        virtual_path = self.resolve_virtual_path(raw_path)
        try:
            return self.paths.virtual_to_container_path(virtual_path)
        except ValueError as exc:
            raise ContainerFileSystemError(str(exc)) from exc

    def _host_path(self, container_path: str) -> Path:
        """容器路径 → 宿主路径;跟随符号链接后必须仍在本会话目录(或全局 skills)内。

        文件工具经 bind mount 在宿主侧读:沙箱里的 /exec 不在 bwrap 内,看得到所有会话的目录,
        agent 在 scratchpad 建一个指向 /sandbox_data 的链接,就能经 view_file 读到别的会话的文件。
        """
        try:
            virtual = self.paths.container_to_virtual_path(container_path)
            return self.paths.virtual_to_host_path(virtual)
        except ValueError as exc:
            raise ContainerFileSystemError(str(exc)) from exc

    async def path_exists(self, container_path: str) -> bool:
        return await asyncio.to_thread(lambda: self._host_path(container_path).exists())

    async def is_directory(self, container_path: str) -> bool:
        return await asyncio.to_thread(lambda: self._host_path(container_path).is_dir())

    async def is_file(self, container_path: str) -> bool:
        return await asyncio.to_thread(lambda: self._host_path(container_path).is_file())

    async def read_container_file(self, container_path: str, *, strict: bool = False) -> str:
        """strict=True 时非 UTF-8 内容抛 UnicodeDecodeError,供要写回的编辑操作使用。"""
        data = await asyncio.to_thread(self._read_host_file, container_path)
        return data.decode("utf-8", errors="strict" if strict else "replace")

    async def list_directory(self, container_path: str) -> str:
        return await asyncio.to_thread(self._list_host_dir, container_path)

    def _read_host_file(self, container_path: str) -> bytes:
        host = self._host_path(container_path)
        try:
            # 路径已解析过;O_NOFOLLOW 拒绝解析之后才换上的符号链接
            fd = os.open(host, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise ContainerFileSystemError(f"Failed to read file: {exc.strerror}") from exc
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise ContainerFileSystemError("Not a regular file")
            data = fh.read(MAX_READ_BYTES + 1)
        if len(data) > MAX_READ_BYTES:
            raise ContainerFileSystemError(
                f"File is larger than {MAX_READ_BYTES // MB} MB; "
                "use bash_tool (head, grep, sed -n) to inspect it"
            )
        return data

    def _list_host_dir(self, container_path: str) -> str:
        host = self._host_path(container_path)
        try:
            with os.scandir(host) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise ContainerFileSystemError(f"Failed to list directory: {exc.strerror}") from exc
        lines = []
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
                name = entry.name + ("/" if stat.S_ISDIR(st.st_mode) else "")
                if stat.S_ISLNK(st.st_mode):
                    name += f" -> {os.readlink(entry.path)}"
            except OSError:
                continue  # 列目录期间被删掉
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            lines.append(f"{stat.filemode(st.st_mode)} {st.st_size:>10} {mtime} {name}")
        return "\n".join(lines) or "(empty directory)"

    def validate_writable_path(self, container_path: str) -> None:
        relative = self._container_path_to_relative(container_path)
        if not relative:
            raise ContainerFileSystemError("Cannot write to session root")
        top_level = relative.split("/")[0]
        if top_level not in WRITABLE_SESSION_SUBDIRS:
            dirs = ", ".join(WRITABLE_SESSION_SUBDIRS)
            raise ContainerFileSystemError(
                f"Path '{container_path}' is not in a writable directory. "
                f"Writable directories: {dirs}"
            )

    async def create_container_file(self, container_path: str, content: str) -> None:
        self.validate_writable_path(container_path)
        if await self.path_exists(container_path):
            if await self.is_directory(container_path):
                raise ContainerFileSystemError(f"Path is a directory: {container_path}")
            raise ContainerFileSystemError(
                f"File already exists: {container_path}. create_file only creates new files; "
                "to change it use str_replace (edit part of it), or bash_tool to rewrite it "
                "entirely (e.g. cat > file <<'EOF'). insert only adds lines, it does not replace."
            )

        await self._write_container_content(container_path, content, mkdir_parents=True)

    async def write_container_file(self, container_path: str, content: str) -> None:
        self.validate_writable_path(container_path)
        if not await self.is_file(container_path):
            raise ContainerFileSystemError(f"File not found: {container_path}")

        await self._write_container_content(container_path, content)

    def _container_path_to_relative(self, container_path: str) -> str:
        try:
            return self.paths.container_to_relative(container_path)
        except ValueError as exc:
            raise ContainerFileSystemError(str(exc)) from exc

    async def _write_container_content(
        self,
        container_path: str,
        content: str,
        *,
        mkdir_parents: bool = False,
    ) -> None:
        # 经宿主侧的 bind mount 写入:没有命令行参数长度限制(base64 作参数在 ~128KB 就会失败)。
        relative = self._container_path_to_relative(container_path)
        try:
            host_path = self.paths.resolve_host_path(relative)  # 跟随符号链接后仍须在会话目录内
            top_level = host_path.relative_to(self.paths.session_root.resolve()).parts[0]
        except (ValueError, IndexError) as exc:
            raise ContainerFileSystemError(f"Path '{container_path}' escapes session") from exc
        if top_level not in WRITABLE_SESSION_SUBDIRS:
            raise ContainerFileSystemError(
                f"Path '{container_path}' is not in a writable directory"
            )
        data = content.encode("utf-8")
        await asyncio.to_thread(self._write_host_file, host_path, data, mkdir_parents)

    @staticmethod
    def _write_host_file(host_path: Path, data: bytes, mkdir_parents: bool) -> None:
        try:
            if mkdir_parents:
                missing = []
                parent = host_path.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                host_path.parent.mkdir(parents=True, exist_ok=True)
                for d in missing:  # agent 用户需要能在新目录里继续写
                    d.chmod(0o777)
            existed = host_path.exists()
            # O_NOFOLLOW:解析之后若末段被换成符号链接则拒绝(缩小 TOCTOU 窗口)
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
            fd = os.open(host_path, flags, 0o666)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            if not existed:
                host_path.chmod(0o666)
        except OSError as exc:
            raise ContainerFileSystemError(f"Failed to write file: {exc}") from exc

    def _workspace_bwrap_args(self) -> list[str]:
        """Bind physical session/skills dirs onto the stable /workspace tree."""
        args: list[str] = ["--dir", self.paths.virtual_root]
        readonly = {"uploads"}

        for name in sorted(SESSION_SUBDIRS):
            physical = f"{self.paths.container_root}/{name}"
            virtual = self.paths.virtual_subdir(name)
            bind_flag = "--ro-bind" if name in readonly else "--bind"
            args.extend([bind_flag, physical, virtual])

        args.extend(
            [
                "--ro-bind",
                CONTAINER_DEFAULT_SKILLS,
                self.paths.virtual_skills,
            ]
        )
        return args

    def _build_bwrap_argv(self) -> list[str]:
        return [
            *self._bwrap_base_argv(self.paths.virtual_scratchpad),
            "bash",
            "--noprofile",
            "--norc",
        ]

    def _bwrap_base_argv(self, home: str) -> list[str]:
        """bwrap 的全部隔离参数(不含要运行的程序)。"""
        tmpfs_size = str(512 * 1024 * 1024)
        return [
            "bwrap",
            "--ro-bind",
            "/usr",
            "/usr",
            # 宿主镜像里 /bin、/sbin 是指向 usr 的符号链接;缺了它们,
            # 以 #!/bin/sh 开头的脚本(如 soffice)会报 "required file not found"
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/sbin",
            "/sbin",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--ro-bind",
            "/etc",
            "/etc",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--size",
            tmpfs_size,
            "--tmpfs",
            "/tmp",
            *self._workspace_bwrap_args(),
            "--chdir",
            self.paths.virtual_scratchpad,
            "--new-session",
            # bwrap 作为新 PID 命名空间的 init,它一死命名空间内所有进程都会被内核杀掉,
            # 这样取消/重置时只需杀 bwrap 就能清掉后台命令(基线 #7)
            "--unshare-pid",
            "--die-with-parent",
            # Clean environment
            "--clearenv",
            "--setenv",
            "HOME",
            home,
            "--setenv",
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
            "--setenv",
            "NODE_PATH",
            "/usr/local/lib/node_modules",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "TERM",
            "xterm-256color",
            "--setenv",
            "LANG",
            "en_US.UTF-8",
        ]

    async def run_isolated(
        self, command: str, timeout: int | None = COMMAND_TIMEOUT_SEC
    ) -> tuple[str, bool]:
        """在一个独立的 bwrap 里跑一条命令,不占用会话 shell 的锁,可与 agent 的命令并行。

        隔离参数与会话 shell 相同(只挂载本会话目录,无网络);HOME 指向这次独有的 /tmp,
        所以 LibreOffice 之类带用户配置的程序不会和 agent shell 里的实例抢同一份配置。
        不经过命令校验和配额检查:只给后端自己拼的固定命令用,不要传入模型给的命令。
        """
        argv = [*self._bwrap_base_argv("/tmp"), "bash", "-c", command]
        prefix = ""
        if settings.SESSION_MAX_FILE_MB:
            prefix = f"ulimit -f {settings.SESSION_MAX_FILE_MB * 1024}; "
        # exec:让 bwrap 直接取代 sh,超时的 KILL 才会落在 bwrap 上(--die-with-parent 随之清掉子进程)
        script = f"{prefix}exec {shlex.join(argv)}"
        try:
            result = await self.docker_client.run_exec(
                self.container_name, script, self.exec_user, timeout=_cap_timeout(timeout)
            )
        except TimeoutError:
            return f"Error: timed out after {_cap_timeout(timeout)} seconds", True
        except Exception as exc:  # 管理器限流(429)或暂时不可用
            logger.warning("Isolated exec failed for session %s: %r", self.paths.session_id, exc)
            return f"Error: sandbox unavailable ({type(exc).__name__})", True
        output = (result.stdout + result.stderr).strip()
        return output, result.returncode != 0

    async def _ensure_started(self, user: str | None = None) -> None:
        exec_user = user or self.exec_user
        if (
            self._process is not None
            and self._process.returncode is None
            and self._active_exec_user == exec_user
        ):
            return
        if self._process is not None:
            await self._stop()

        self._active_exec_user = exec_user
        self._current_cwd = self.paths.virtual_scratchpad
        self._output_queue = asyncio.Queue()
        self._process = await self.docker_client.spawn(
            self.container_name, self._build_bwrap_argv(), user=exec_user
        )
        self._start_readers()
        await asyncio.sleep(0.2)
        if self._process.returncode is not None:
            errors = await self._collect_recent_stderr()
            errors = errors or getattr(self._process, "close_reason", None) or "unknown error"
            raise RuntimeError(f"Bash session failed to start: {errors}")
        await self._init_shell_umask()

    async def _init_shell_umask(self) -> None:
        """Group-writable files so dev can manage agent-created artifacts."""
        script = "umask 0002\n"
        if settings.SESSION_MAX_FILE_MB:
            # 同时设软硬上限,agent 无法再调高;超限的写入会让对应进程收到 SIGXFSZ
            script += f"ulimit -f {settings.SESSION_MAX_FILE_MB * 1024}\n"
        await self.docker_client.write_stdin(self._process, script)
        await asyncio.sleep(0.05)
        await self._drain_queue()

    async def _collect_recent_stderr(self) -> str:
        lines: list[str] = []
        while True:
            try:
                label, line = self._output_queue.get_nowait()
                if label == "stderr" and line is not None:
                    lines.append(line.rstrip("\n"))
            except asyncio.QueueEmpty:
                break
        return "\n".join(lines)

    def _start_readers(self) -> None:
        self._reader_tasks = []
        if self._process is None:
            return
        for stream, label in (
            (self._process.stdout, "stdout"),
            (self._process.stderr, "stderr"),
        ):
            if stream is None:
                continue
            task = asyncio.create_task(self._read_stream(stream, label))
            self._reader_tasks.append(task)

    async def _read_stream(self, stream: StreamReader, label: str) -> None:
        try:
            while True:
                try:
                    line = await stream.readline()
                except ValueError:
                    # 单行超过 StreamReader 的上限:按输出超限处理,不能让读取任务悄悄结束、命令空等到超时
                    self._output_exceeded = True
                    break
                if not line:
                    break
                self._output_bytes += len(line)
                if self._output_bytes > MAX_COMMAND_OUTPUT_BYTES:
                    self._output_exceeded = True
                    break  # 不再读取;_read_until_sentinel 会发现标记并杀掉 shell
                await self._output_queue.put((label, line.decode(errors="replace")))
        finally:
            self._output_queue.put_nowait((label, None))

    async def _stop(self) -> None:
        for task in self._reader_tasks:
            task.cancel()
        for task in self._reader_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # 读取任务已因异常(如超长行)结束,不能让它挡住进程清理
                logger.warning(f"sandbox reader task failed before stop: {exc!r}")
        self._reader_tasks = []

        if self._process is not None:
            try:
                await self.docker_client.terminate(self._process)
            finally:
                self._process = None
        await self._drain_queue()

    async def _drain_queue(self) -> None:
        while True:
            try:
                self._output_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _wrap_command(self, command: str) -> str:
        # Wrap in a command group so </dev/null applies after the full command.
        # Appending </dev/null to the last line breaks heredoc terminators (e.g. PY).
        self._sentinel = f"{BASH_SENTINEL}{secrets.token_hex(8)}"
        return (
            f"{{\n{command.rstrip()}\n}} </dev/null\n"
            f"printf '{self._sentinel}:%s:%s\\n' $? \"$PWD\"\n"
        )

    def _cwd_within_session(self, cwd: str) -> bool:
        root = self.paths.virtual_root.rstrip("/")
        normalized = cwd.rstrip("/")
        return normalized == root or normalized.startswith(f"{root}/")

    def _parse_sentinel_line(self, line: str) -> tuple[str | None, str | None, int]:
        """返回 (标记前的输出, cwd, 退出码)。"""
        if self._sentinel not in line:
            return None, None, 0
        before, _, after = line.partition(self._sentinel)
        cwd = None
        code = 0
        if after.startswith(":"):
            parts = after[1:].split(":", 1)
            if len(parts) == 2:
                cwd = parts[1]
                code = int(parts[0]) if parts[0].isdigit() else 0
        output_before = before.rstrip() if before else None
        return output_before, cwd, code

    async def _read_until_sentinel(self, timeout: float) -> tuple[str, bool, str | None]:
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        cwd: str | None = None
        code = 0
        ended = 0  # 已读完(EOF)的流数;全部读完说明 shell 已经退出
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._output_exceeded or getattr(self._process, "output_limit_exceeded", False):
                raise OutputLimitExceeded
            if ended and ended >= len(self._reader_tasks):
                raise ShellExited(getattr(self._process, "returncode", None))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                label, line = await asyncio.wait_for(self._output_queue.get(), timeout=remaining)
            except TimeoutError:
                continue
            if line is None:
                ended += 1
                continue  # 回到循环开头:先看是否超出输出上限,再看 shell 是否已退出

            stripped = line.rstrip("\n")
            if self._sentinel in stripped:
                before, parsed_cwd, code = self._parse_sentinel_line(stripped)
                cwd = parsed_cwd
                if before:
                    if label == "stdout":
                        stdout_lines.append(before)
                    else:
                        stderr_lines.append(before)
                break

            if label == "stdout":
                stdout_lines.append(stripped)
            else:
                stderr_lines.append(stripped)
        else:
            await self._stop()
            await self._ensure_started(self._active_exec_user)
            return "", True, None

        output = self._beautify_output(stdout_lines, stderr_lines)
        if code:
            # 非零退出不算工具出错(grep 没匹配也是 1),但要让模型知道,否则静默失败的命令看起来像成功
            output = f"{output}\nExit code: {code}" if output else f"Exit code: {code}"
        return output, False, cwd

    def _beautify_output(self, stdout_lines: list[str], stderr_lines: list[str]) -> str:
        parts: list[str] = []
        template = "====================== {label} ======================\n\n{content}\n\n"
        if stdout_lines:
            content = self._truncate_lines(stdout_lines, "stdout", keep_head=True)
            parts.append(template.format(label="stdout", content=content))
        if stderr_lines:
            content = self._truncate_lines(stderr_lines, "stderr", keep_head=False)
            parts.append(template.format(label="stderr", content=content))
        return "\n".join(parts)

    def _spill(self, lines: list[str], label: str) -> str | None:
        """超长输出的完整内容写到 scratchpad,返回沙箱内路径;写失败返回 None(只截断)。"""
        self._spill_count += 1
        # scratchpad 对 agent 可写:名字可预测时它能预先放一个同名符号链接,让后端(root)顺着写出沙箱。
        # 随机后缀 + O_EXCL(已存在就失败)+ O_NOFOLLOW,只会新建普通文件
        name = f"bash_{label}_{self._spill_count}_{secrets.token_hex(4)}.txt"
        text = "\n".join(lines)[:SPILL_MAX_CHARS]
        try:
            target = self.paths.scratchpad / name
            target.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            fd = os.open(target, flags, 0o666)
            os.fchmod(fd, 0o666)  # 不受 umask 影响,agent 用户也能读写
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            logger.warning("Session %s spill failed: %s", self.paths.session_id, exc)
            return None
        return f"{self.paths.virtual_scratchpad}/{name}"

    def _truncate_lines(self, lines: list[str], label: str, *, keep_head: bool) -> str:
        if len(lines) <= OUTPUT_MAX_LINES:
            return "\n".join(lines)
        omitted = len(lines) - OUTPUT_MAX_LINES
        saved = self._spill(lines, label)
        hint = (
            f" Full output saved to {saved} (use grep / view_file with view_range to read it)."
            if saved
            else ""
        )
        note = (
            f"... Output truncated ({omitted} lines omitted, {len(lines)} total lines).{hint} ..."
        )
        if keep_head:
            return "\n".join(lines[:OUTPUT_MAX_LINES]) + f"\n\n{note}"
        return f"{note}\n\n" + "\n".join(lines[-OUTPUT_MAX_LINES:])


class BashSessionsManager:
    """Manages session folders and BashSession instances."""

    def __init__(
        self,
        host_sessions_root: Path | None = None,
        container_name: str = CONTAINER_NAME,
    ) -> None:
        self.host_sessions_root = host_sessions_root or SESSION_ROOT
        self.container_name = container_name
        self._sessions: dict[str, BashSession] = {}
        self._lock = asyncio.Lock()
        self._validator = CommandValidator()
        self._docker_client = ManagerClient()

    async def _create_locked(self, sid: str, exec_user: str = CONTAINER_AGENT_USER) -> BashSession:
        paths = SessionPaths.create(sid, self.host_sessions_root)
        await self._provision_folders(paths)
        session = BashSession(
            paths,
            self.container_name,
            self._validator,
            self._docker_client,
            exec_user=exec_user,
        )
        self._sessions[sid] = session
        return session

    async def get_or_create(self, session_id: str) -> BashSession:
        # 查和建在同一把锁里:并行的工具调用首次用到同一会话时不会重复创建
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = await self._create_locked(session_id)
            session.last_used = time.monotonic()
        await self.reap_idle_shells(keep=session_id)
        return session

    async def reap_idle_shells(self, keep: str | None = None) -> int:
        """关闭空闲 shell,返回关闭个数。

        每个会话的 shell 常驻一个 bwrap 进程并占用 sandbox-manager 的一个 spawn 名额
        (MANAGER_MAX_SPAWN),不回收的话会话一多新会话就起不了 shell。两条规则:
        空闲超过 SANDBOX_SHELL_IDLE_SEC 的关闭;仍存活的超过 SANDBOX_MAX_SHELLS 时按最久未用关闭。
        正在执行命令的不动;关闭后下次命令自动重启,cwd 回到 scratchpad。
        """
        idle_sec = settings.SANDBOX_SHELL_IDLE_SEC
        max_shells = settings.SANDBOX_MAX_SHELLS
        now = time.monotonic()
        alive = sorted(
            (s for sid, s in list(self._sessions.items()) if sid != keep and s.shell_alive),
            key=lambda s: s.last_used,
        )
        # keep 这次马上要用,也占一个名额
        excess = len(alive) + 1 - max_shells if max_shells else 0
        closed = 0
        for session in alive:
            idle = idle_sec and now - session.last_used > idle_sec
            if (idle or closed < excess) and await session.close_if_idle():
                closed += 1
                logger.info("Closed idle sandbox shell of session %s", session.paths.session_id)
        return closed

    async def close_all(self) -> None:
        """进程退出时关闭所有 shell,不删文件。"""
        async with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            try:
                await session.close()
            except Exception:
                logger.exception("Closing sandbox shell of %s failed", session.paths.session_id)

    async def purge_session(self, session_id: str) -> None:
        """彻底删除一个会话的沙箱数据;即使进程重启后内存里已没有该会话也能工作。"""
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.close()
        paths = SessionPaths.create(session_id, self.host_sessions_root)
        await asyncio.to_thread(shutil.rmtree, paths.session_root, True)
        # 宿主侧删不干净(文件属主不一致等)时,再让 manager 以 root 清一遍
        if paths.session_root.exists():
            await self._docker_client.remove_session(self.container_name, paths.container_root)

    async def bash_tool_helper(
        self,
        session_id: str,
        command: str | None = None,
        timeout: int | None = None,
        restart: bool = False,
    ) -> tuple[str, bool]:
        session = await self.get_or_create(session_id)

        if restart:
            message = await session.reset()
            return message, False

        if not command:
            return "Error: Missing 'command' in tool input", True

        return await session.execute(command, timeout=timeout)

    async def snapshot_session(self, session_id: str, prompt_id: str) -> bool:
        """每轮开始前给 output / scratchpad 做一份快照,供回退时恢复;成功返回 True。

        快照放在会话目录的 snapshots/ 下(不在 bwrap 挂载里,agent 的命令碰不到),
        由容器内的 agent 用户复制。scratchpad 下的转换中间目录(SNAPSHOT_EXCLUDED)不复制。
        内容与最新一份快照相同(路径、类型、权限、大小、mtime 一致)时只建硬链接副本,
        不再复制。合计超过 SNAPSHOT_MAX_MB 就跳过,只保留最近 SNAPSHOT_KEEP 份。
        先写到 .tmp_ 目录再改名,复制失败不会留下残缺的快照。
        """
        if not settings.SNAPSHOT_KEEP:
            return False
        session = await self.get_or_create(session_id)
        paths = session.paths
        size, fingerprint = await asyncio.to_thread(snapshot_manifest, paths.session_root)
        if size > settings.SNAPSHOT_MAX_MB * MB:
            logger.info("Session %s too large to snapshot (%d MB)", session_id, size // MB)
            return False
        root = paths.container_root
        name = shlex.quote(snapshot_name(prompt_id))
        tmp = shlex.quote(f".tmp_{snapshot_name(prompt_id, suffix_only=True)}")
        excludes = " ".join(f"--exclude=scratchpad/{d}" for d in SNAPSHOT_EXCLUDED)
        script = (
            "set -e; set -o pipefail; export LC_ALL=C; "
            f"mkdir -p {root}/snapshots; cd {root}/snapshots; rm -rf .tmp_*; "
            "L=$(ls -1 | tail -n 1); "
            f'if [ -n "$L" ] && [ "$(cat "$L"/.manifest 2>/dev/null)" = {fingerprint} ]; then '
            f'cp -al "$L" {tmp}; '
            f"else mkdir {tmp}; "
            f"tar -C {root} --anchored {excludes} -cf - output scratchpad | tar -C {tmp} -xpf -; "
            f"echo {fingerprint} > {tmp}/.manifest; fi; "
            f"mv {tmp} {name}; "
            f"ls -1 | head -n -{settings.SNAPSHOT_KEEP} | xargs -r rm -rf"
        )
        try:
            result = await self._docker_client.run_exec(self.container_name, script)
        except Exception:
            logger.exception("Snapshot of session %s failed", session_id)
            return False
        if result.returncode != 0:
            logger.warning("Snapshot of session %s failed: %s", session_id, result.stderr.strip())
            return False
        return True

    async def restore_snapshot(self, session_id: str, prompt_id: str) -> bool:
        """把 output / scratchpad 恢复成该轮开始前的样子,并丢弃这一轮及之后的快照。

        没有对应快照(过期被淘汰、当时太大、旧会话)返回 False,文件保持现状。
        """
        session = await self.get_or_create(session_id)
        root = session.paths.container_root
        suffix = shlex.quote(snapshot_name(prompt_id, suffix_only=True))
        script = (
            "export LC_ALL=C; "
            f"cd {root}/snapshots 2>/dev/null || exit 3; "
            f'T=$(ls -1 | grep -F -- {suffix} | tail -n 1); [ -n "$T" ] || exit 3; '
            "set -e; "
            f"find {root}/output -mindepth 1 -delete; find {root}/scratchpad -mindepth 1 -delete; "
            f'cp -a "$T"/output/. {root}/output/; cp -a "$T"/scratchpad/. {root}/scratchpad/; '
            "ls -1 | awk -v t=\"$T\" '$0 >= t' | xargs -r rm -rf"
        )
        async with session._lock:
            # 命令行里的工作目录可能在被清掉的子目录里,先关掉 shell,下次执行时会重新启动
            await session._stop()
            try:
                result = await self._docker_client.run_exec(self.container_name, script)
            except Exception:
                logger.exception("Restoring snapshot of session %s failed", session_id)
                return False
        if result.returncode == 3:
            return False
        if result.returncode != 0:
            logger.warning("Restoring session %s failed: %s", session_id, result.stderr.strip())
            return False
        return True

    async def _provision_folders(self, paths: SessionPaths) -> None:
        self.host_sessions_root.mkdir(parents=True, exist_ok=True)
        paths.session_root.mkdir(parents=True, exist_ok=True)
        for subdir in paths.subdirs:
            subdir.mkdir(parents=True, exist_ok=True)
        await self._docker_client.provision_session(self.container_name, paths.container_root)
