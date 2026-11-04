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
