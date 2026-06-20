"""finalcheck 单测：成稿终检去重（#2）+ 信息密度地板（#4-A）。

降级铁律是核心契约：embed 不可用 / 空输入 → 原样透传，绝不抛异常。
patch 目标按「使用处」打桩：digest.finalcheck.embed_titles。
"""

from __future__ import annotations

import digest.finalcheck as fc
from digest.finalcheck import dedup_secondary, density_floor, split_items


# ──────────────────────────────────────────────────────────────────────
# dedup_secondary
# ──────────────────────────────────────────────────────────────────────


def test_dedup_passthrough_when_embed_returns_none(monkeypatch):
    # Arrange
    monkeypatch.setattr(fc, "embed_titles", lambda titles: None)
    gaps = [{"title": "scout A"}, {"title": "scout B"}]
    bio = {"title": "bio X"}

    # Act
    kept_gaps, kept_bio = dedup_secondary(["news 1"], gaps, bio)

    # Assert
    assert kept_gaps == gaps
    assert kept_bio == bio


def test_dedup_passthrough_when_no_news_titles(monkeypatch):
    # Arrange — 空基准应在调用 embed 之前就透传
    called = {"n": 0}

    def _spy(titles):
        called["n"] += 1
        return None

    monkeypatch.setattr(fc, "embed_titles", _spy)
    gaps = [{"title": "scout A"}]
    bio = {"title": "bio X"}

    # Act
    kept_gaps, kept_bio = dedup_secondary([], gaps, bio)

    # Assert
    assert kept_gaps == gaps
    assert kept_bio == bio
    assert called["n"] == 0


def test_dedup_removes_high_similarity_gap_and_bio(monkeypatch):
    # Arrange — base 全是单位向量 [1,0]；候选若标题含 "DUP" 则同向(撞题)，否则正交。
    def fake_embed(titles):
        out = []
        for t in titles:
            if t == "base":
                out.append([1.0, 0.0])
            elif "DUP" in t:
                out.append([1.0, 0.0])  # cos=1.0 ≥ 阈值 → 剔除
            else:
                out.append([0.0, 1.0])  # cos=0.0 < 阈值 → 保留
        return out

    monkeypatch.setattr(fc, "embed_titles", fake_embed)
    gaps = [{"title": "DUP scout"}, {"title": "fresh scout"}]
    bio = {"title": "DUP bio"}

    # Act
    kept_gaps, kept_bio = dedup_secondary(["base"], gaps, bio)

    # Assert
    assert kept_gaps == [{"title": "fresh scout"}]
    assert kept_bio is None


def test_dedup_keeps_all_when_low_similarity(monkeypatch):
    # Arrange — base=[1,0]，所有候选正交 → 全部保留
    def fake_embed(titles):
        return [[1.0, 0.0] if t == "base" else [0.0, 1.0] for t in titles]

    monkeypatch.setattr(fc, "embed_titles", fake_embed)
    gaps = [{"title": "g1"}, {"title": "g2"}]
    bio = {"title": "b1"}

    # Act
    kept_gaps, kept_bio = dedup_secondary(["base"], gaps, bio)

    # Assert
    assert kept_gaps == gaps
    assert kept_bio == bio


def test_dedup_handles_no_candidates(monkeypatch):
    # Arrange — 有基准但无 gaps 无 bio
    monkeypatch.setattr(fc, "embed_titles", lambda titles: [[1.0, 0.0]])

    # Act
    kept_gaps, kept_bio = dedup_secondary(["base"], [], None)

    # Assert
    assert kept_gaps == []
    assert kept_bio is None


# ──────────────────────────────────────────────────────────────────────
# split_items
# ──────────────────────────────────────────────────────────────────────


SAMPLE_DIGEST = """## 🌍 国际要闻
**🔥 第一条标题：美伊协议剖析**
- **【核心事实】**：第一段正文内容。
- **【深层逻辑】**：第二段正文内容。
> 📰 来源：BBC World（https://example.com/a）

**⭐⭐⭐ 🎯 第二条标题：阿里开源向量库**
- **【核心事实】**：阿里在 GitHub 开源 zvec。
> 📰 来源：GitHub Trending（原文链接）
"""


def test_split_items_parses_well_formed_digest():
    # Act
    items = split_items(SAMPLE_DIGEST)

    # Assert
    assert len(items) == 2
    assert items[0]["title"] == "第一条标题：美伊协议剖析"
    assert "第一段正文内容" in items[0]["body"]
    assert "第二段正文内容" in items[0]["body"]
    assert "📰 来源" in items[0]["source_line"]

    # 第二条标题剥掉了多 emoji 与中间空格
    assert items[1]["title"] == "第二条标题：阿里开源向量库"
    assert "zvec" in items[1]["body"]


def test_split_items_returns_empty_on_no_titles():
    # Act / Assert
    assert split_items("普通段落，没有任何加粗标题行。\n第二行。") == []


def test_split_items_returns_empty_on_blank():
    # Act / Assert
    assert split_items("") == []


# ──────────────────────────────────────────────────────────────────────
# density_floor
# ──────────────────────────────────────────────────────────────────────


def test_density_floor_flags_low_density_item():
    # Arrange — 正文极短且无任何数字/专有名词
    items = [{"title": "空话条目", "body": "这条点评写得很空。", "source_line": ""}]

    # Act
    bad = density_floor(items)

    # Assert
    assert "空话条目" in bad


def test_density_floor_passes_high_density_item():
    # Arrange — 长正文 + 硬数字($2 billion / 40%) + 多个专有名词(OpenAI/Microsoft)
    body = (
        "OpenAI 与 Microsoft 宣布达成 $2 billion 的合作协议，"
        "该交易将使云算力提升 40%。分析师 Reuters 指出，"
        "这笔投资远超此前 Google 的同类布局，"
        "标志着 AI 基础设施竞赛进入新阶段，影响深远且具体。" * 2
    )
    items = [{"title": "硬核条目", "body": body, "source_line": ""}]

    # Act
    bad = density_floor(items)

    # Assert
    assert "硬核条目" not in bad


def test_density_floor_disabled_returns_empty(monkeypatch):
    # Arrange
    monkeypatch.setattr(fc, "DENSITY_FLOOR_ENABLED", False)
    items = [{"title": "随便", "body": "短", "source_line": ""}]

    # Act / Assert
    assert density_floor(items) == []
