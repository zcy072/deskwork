"""上传文件名归一化 / 去重。"""

from app.utils.filename import normalize_upload_filename, unique_upload_name


def test_normalize_replaces_spaces_and_parens():
    assert normalize_upload_filename("my report (final).pdf") == "my_report_final.pdf"


def test_normalize_strips_directories():
    assert normalize_upload_filename("../../etc/passwd") == "passwd"
    assert normalize_upload_filename("a/b/c.txt") == "c.txt"


def test_normalize_empty_stem_falls_back():
    assert normalize_upload_filename("().txt") == "upload.txt"


def test_unique_name_no_collision(tmp_path):
    assert unique_upload_name(tmp_path, "a.txt") == "a.txt"


def test_unique_name_avoids_existing_and_reserved(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert unique_upload_name(tmp_path, "a.txt") == "a_2.txt"
    assert unique_upload_name(tmp_path, "a.txt", reserved={"a_2.txt"}) == "a_3.txt"


def test_normalize_removes_invisible_and_spoofing_characters():
    """NUL 会让保存报 400,ANSI 转义和 RLO 会在界面和模型上下文里伪装文件名。"""
    assert normalize_upload_filename("a\x00b.txt") == "ab.txt"
    assert normalize_upload_filename("invoice‮fdp.exe") == "invoicefdp.exe"
    assert "\x1b" not in normalize_upload_filename("\x1b[31mred.txt")


def test_normalize_avoids_hidden_files_options_and_shell_metacharacters():
    assert normalize_upload_filename(".bashrc") == "bashrc"
    assert normalize_upload_filename("-rf.txt") == "rf.txt"
    assert normalize_upload_filename("$(rm).txt") == "rm.txt"
    assert normalize_upload_filename("a;b`c`.txt") == "a_b_c.txt"
    assert normalize_upload_filename("...") == "upload"


def test_normalize_caps_utf8_length_and_keeps_suffix():
    """超长文件名在磁盘上报 ENAMETOOLONG,原来会变成 500。"""
    for raw, suffix in (("x" * 300 + ".pdf", ".pdf"), ("中" * 100 + ".xlsx", ".xlsx")):
        name = normalize_upload_filename(raw)
        assert len(name.encode("utf-8")) <= 200 and name.endswith(suffix)
    assert normalize_upload_filename("报告 2026(终稿).docx") == "报告_2026_终稿.docx"
