"""bash 超长输出:截断显示,完整内容落盘到 scratchpad。"""

from app.sandbox.bash_handler import BashSession
from app.sandbox.constants import OUTPUT_MAX_CHARS, OUTPUT_MAX_LINES
from app.sandbox.session_paths import SessionPaths


def _session(tmp_path) -> BashSession:
    paths = SessionPaths.create("spill-test", sessions_root=tmp_path)
    paths.scratchpad.mkdir(parents=True)
    return BashSession(paths, manager_client=object())


def test_short_output_untouched(tmp_path):
    s = _session(tmp_path)
    assert s._beautify_output(["a", "b"], []).count("truncated") == 0
    assert not list(s.paths.scratchpad.iterdir())


def test_long_stdout_keeps_head_and_spills_full_text(tmp_path):
    s = _session(tmp_path)
    lines = [f"line{i}" for i in range(OUTPUT_MAX_LINES + 50)]
    out = s._beautify_output(lines, [])
    assert "line0" in out and f"line{OUTPUT_MAX_LINES - 1}" in out
    assert f"line{OUTPUT_MAX_LINES}\n" not in out
    (spilled,) = s.paths.scratchpad.glob("bash_stdout_1_*.txt")
    assert f"/workspace/scratchpad/{spilled.name}" in out
    assert spilled.read_text(encoding="utf-8").splitlines() == lines
    assert spilled.stat().st_mode & 0o777 == 0o666


def test_long_stderr_keeps_tail(tmp_path):
    s = _session(tmp_path)
    lines = [f"err{i}" for i in range(OUTPUT_MAX_LINES + 10)]
    out = s._beautify_output([], lines)
    assert f"err{len(lines) - 1}" in out and "err0\n" not in out
    assert "/workspace/scratchpad/bash_stderr_1_" in out


def test_spill_failure_still_truncates(tmp_path, monkeypatch):
    s = _session(tmp_path)
    monkeypatch.setattr(s, "_spill", lambda lines, label: None)
    out = s._beautify_output([f"x{i}" for i in range(OUTPUT_MAX_LINES + 5)], [])
    assert "Output truncated" in out and "Full output saved" not in out


def test_spill_never_follows_planted_symlink(tmp_path):
    """agent 预先在 scratchpad 放同名符号链接,后端也不能顺着写到沙箱外。"""
    s = _session(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    for label in ("stdout", "stderr"):
        (s.paths.scratchpad / f"bash_{label}_1.txt").symlink_to(victim)
    s._beautify_output([f"x{i}" for i in range(OUTPUT_MAX_LINES + 5)], [])
    assert victim.read_text(encoding="utf-8") == "keep"
    assert len(list(s.paths.scratchpad.glob("bash_stdout_1_*.txt"))) == 1


def test_single_huge_line_is_capped_by_characters(tmp_path):
    # 压缩 JSON / base64 只有一行,只按行数截不住,会把几 MB 塞给模型
    s = _session(tmp_path)
    line = "A" * (OUTPUT_MAX_CHARS * 10)
    out = s._beautify_output([line], [])
    assert len(out) < OUTPUT_MAX_CHARS + 1000
    assert "Output truncated" in out
    (spilled,) = s.paths.scratchpad.glob("bash_stdout_1_*.txt")
    assert spilled.read_text(encoding="utf-8") == line


def test_stderr_char_cap_keeps_tail(tmp_path):
    s = _session(tmp_path)
    out = s._beautify_output([], ["x" * OUTPUT_MAX_CHARS * 2 + "END"])
    assert "END" in out and len(out) < OUTPUT_MAX_CHARS + 1000
