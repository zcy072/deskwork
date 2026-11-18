from app.utils.text_decoding import decode_text


def test_utf8_and_bom():
    assert decode_text("地区,销量\n".encode()) == "地区,销量\n"
    assert decode_text(b"\xef\xbb\xbf" + "地区".encode()) == "地区"


def test_gbk_chinese_csv_is_readable():
    assert decode_text("地区,销量\n华东,120\n".encode("gbk")) == "地区,销量\n华东,120\n"


def test_non_chinese_garbage_falls_back_to_utf8_replace():
    out = decode_text(bytes([0x80, 0x81, 0xFE, 0xFF, 0x41]))
    assert out.endswith("A") and "�" in out
