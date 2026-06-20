"""跨期事件串联（digest/linkage.py）+ sidecar 持久化（digest/storage.py）单元测试。

降级铁律覆盖：embed 不可用 / 无历史 → 不写 progress_of、不抛错。
正路覆盖：高相似命中写正确 progress_of；低相似不写。
持久化：save_reps_sidecar → load_recent_reps 往返一致。
patch 一律打在「使用处」（digest.linkage.* ），而非定义处。
"""

from __future__ import annotations

from digest.linkage import tag_progress
from digest import storage


# ── 一条标准化向量（L2 归一化后余弦 = 点积）──
_V_A = [1.0, 0.0, 0.0]   # 与自身余弦 = 1.0（高相似）
_V_B = [0.0, 1.0, 0.0]   # 与 _V_A 余弦 = 0.0（低相似）


def _one_past_entry():
    return [{"raw_title": "Fed raises rates", "zh": "美联储加息", "emoji": "📈", "date": "2026-06-18"}]


def test_embed_unavailable_writes_no_progress(monkeypatch):
    # Arrange
    monkeypatch.setattr("digest.linkage.load_recent_reps", _one_past_entry)
    monkeypatch.setattr("digest.linkage.embed_titles", lambda titles: None)
    reps = [{"title": "Fed raises rates again"}]

    # Act
    tag_progress(reps)

    # Assert
    assert "progress_of" not in reps[0]


def test_no_history_returns_without_progress(monkeypatch):
    # Arrange
    monkeypatch.setattr("digest.linkage.load_recent_reps", lambda: [])
    # embed_titles 不该被调用；若被调用返回可命中向量，确保仍因无历史而不写
    monkeypatch.setattr("digest.linkage.embed_titles", lambda titles: [_V_A for _ in titles])
    reps = [{"title": "Fed raises rates again"}]

    # Act
    tag_progress(reps)

    # Assert
    assert "progress_of" not in reps[0]


def test_high_similarity_sets_progress_of(monkeypatch):
    # Arrange
    past = _one_past_entry()
    monkeypatch.setattr("digest.linkage.load_recent_reps", lambda: past)
    # today 向量与 past 向量都用 _V_A → 余弦 1.0 ≥ 0.80 阈值
    monkeypatch.setattr("digest.linkage.embed_titles", lambda titles: [_V_A for _ in titles])
    reps = [{"title": "Fed raises rates again"}]

    # Act
    tag_progress(reps)

    # Assert
    assert reps[0]["progress_of"] == {"prev_zh": "美联储加息", "date": "2026-06-18"}


def test_low_similarity_no_progress(monkeypatch):
    # Arrange
    past = _one_past_entry()
    monkeypatch.setattr("digest.linkage.load_recent_reps", lambda: past)

    # today 用 _V_B，past 用 _V_A → 余弦 0.0 < 0.80
    def fake_embed(titles):
        # linkage 先 embed today（len==1），再 embed past（len==1）；按内容区分
        if titles == ["Fed raises rates"]:
            return [_V_A]
        return [_V_B for _ in titles]

    monkeypatch.setattr("digest.linkage.embed_titles", fake_embed)
    reps = [{"title": "Unrelated tech launch"}]

    # Act
    tag_progress(reps)

    # Assert
    assert "progress_of" not in reps[0]


def test_sidecar_roundtrip(tmp_path, monkeypatch):
    # Arrange：切到临时目录，sidecar 写到 tmp 的 digests/meta/ 下
    monkeypatch.chdir(tmp_path)
    reps = [{"title": "Quantum chip breakthrough"}]

    # Act
    storage.save_reps_sidecar(reps)
    loaded = storage.load_recent_reps()

    # Assert
    assert len(loaded) == 1
    entry = loaded[0]
    assert entry["raw_title"] == "Quantum chip breakthrough"
    assert entry["zh"] == ""        # 写时通常无 zh，存空串
    assert entry["emoji"] == ""
    assert "date" in entry and len(entry["date"]) == 10


def test_load_recent_reps_no_dir_returns_empty(tmp_path, monkeypatch):
    # Arrange：空临时目录，无 digests/meta
    monkeypatch.chdir(tmp_path)

    # Act
    loaded = storage.load_recent_reps()

    # Assert
    assert loaded == []
