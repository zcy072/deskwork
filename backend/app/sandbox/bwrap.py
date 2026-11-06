"""bwrap 隔离参数。会话 shell 和一次性命令(run_isolated)共用同一套,集中在这里便于审查。

只拼 argv,不启动进程;argv 经 sandbox-manager 的 /spawn 或 /exec 在沙箱容器内执行。
"""

from app.sandbox.constants import CONTAINER_DEFAULT_SKILLS, SESSION_SUBDIRS
from app.sandbox.session_paths import SessionPaths

TMPFS_SIZE = 512 * 1024 * 1024

# 只读挂载的会话子目录;其余子目录可写
READONLY_SUBDIRS = frozenset({"uploads"})


def workspace_binds(paths: SessionPaths) -> list[str]:
    """把物理会话目录和全局 skills 挂到固定的 /workspace 树上。"""
    args: list[str] = ["--dir", paths.virtual_root]
    for name in sorted(SESSION_SUBDIRS):
        physical = f"{paths.container_root}/{name}"
        bind_flag = "--ro-bind" if name in READONLY_SUBDIRS else "--bind"
        args.extend([bind_flag, physical, paths.virtual_subdir(name)])
    args.extend(["--ro-bind", CONTAINER_DEFAULT_SKILLS, paths.virtual_skills])
    return args


def base_argv(paths: SessionPaths, home: str) -> list[str]:
    """bwrap 的全部隔离参数(不含要运行的程序)。"""
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
        str(TMPFS_SIZE),
        "--tmpfs",
        "/tmp",
        *workspace_binds(paths),
        "--chdir",
        paths.virtual_scratchpad,
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


def shell_argv(paths: SessionPaths) -> list[str]:
    """会话常驻 shell:HOME 为 scratchpad,不读 profile/rc。"""
    return [*base_argv(paths, paths.virtual_scratchpad), "bash", "--noprofile", "--norc"]


def oneshot_argv(paths: SessionPaths, command: str) -> list[str]:
    """一次性命令:HOME 指向这次独有的 /tmp,不和会话 shell 共享用户配置。"""
    return [*base_argv(paths, "/tmp"), "bash", "-c", command]
