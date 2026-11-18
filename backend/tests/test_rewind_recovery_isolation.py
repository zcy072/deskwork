"""一条坏的回退记录不能挡住其他会话的恢复。"""

import json

from app.services import rewind


async def test_bad_journal_does_not_block_others(tmp_path, monkeypatch):
    for sid in ("a", "b"):
        d = tmp_path / sid / "operations" / "r1"
        d.mkdir(parents=True)
        (d / "journal.json").write_text(
            "{not json" if sid == "a" else json.dumps({"session_id": sid})
        )
    seen = []

    async def fake(journal):
        seen.append(journal.parent.parent.parent.name)
        if journal.parent.parent.parent.name == "a":
            json.loads(journal.read_text())

    monkeypatch.setattr(rewind, "_recover_journal", fake)
    await rewind.recover_operations(tmp_path)
    assert sorted(seen) == ["a", "b"]
    assert (tmp_path / "a" / "operations" / "r1" / "journal.json").exists()
