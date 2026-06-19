"""语义去重单元测试。

核心约束：不依赖 fastembed / 不联网 / 不下载模型。
做法：merge_similar_clusters 接受注入的 embed_fn，测试喂入手造向量。
真实 embed_titles 只测「无库 / 空输入时优雅返回 None」这条降级路径。
"""

from __future__ import annotations

from digest.scoring import merge_similar_clusters
from digest.embedding import embed_titles


def _rep(title, score, source, reference=False, cluster_size=1):
    """构造一个「簇代表作」dict，字段对齐 cluster_and_boost 的输出。"""
    return {
        "title": title, "summary": "", "score": float(score),
        "source": source, "category": "国际", "reference": reference,
        "link": f"http://{source}.com", "cluster_size": cluster_size,
        "cluster_titles": [],
    }


def _fake_embed(vectors):
    """返回一个 embed_fn：忽略入参 titles，按顺序吐预设向量。"""
    return lambda titles: vectors


class TestMergeSimilarClusters:
    def test_passthrough_when_embed_unavailable(self):
        """embed_fn 返回 None（库缺失模拟）→ 原样返回，绝不中断。"""
        reps = [_rep("A", 10, "BBC"), _rep("B", 8, "DW")]
        result = merge_similar_clusters(reps, threshold=0.86,
                                        embed_fn=lambda t: None)
        assert result == reps

    def test_fewer_than_two_reps_passthrough(self):
        """不足 2 条无需比较，原样返回，且不触发 embedding。"""
        reps = [_rep("only one", 10, "BBC")]
        called = {"n": 0}

        def embed(t):
            called["n"] += 1
            return [[1.0, 0.0]]

        result = merge_similar_clusters(reps, threshold=0.86, embed_fn=embed)
        assert result == reps
        assert called["n"] == 0

    def test_merges_semantically_similar(self):
        """两条向量近乎相同（cos≈1）→ 合并为 1 簇，cluster_size 累加。"""
        reps = [_rep("Fed holds rates", 10, "BBC", cluster_size=1),
                _rep("Powell keeps rates unchanged", 8, "DW", cluster_size=1)]
        vecs = [[1.0, 0.0], [1.0, 0.0]]
        result = merge_similar_clusters(reps, threshold=0.86,
                                        embed_fn=_fake_embed(vecs))
        assert len(result) == 1, f"应合并为 1 簇，实际 {len(result)}"
        assert result[0]["cluster_size"] == 2

    def test_does_not_merge_dissimilar(self):
        """正交向量（cos=0）→ 保持两簇。"""
        reps = [_rep("Fed rate decision", 10, "BBC"),
                _rep("Tokyo earthquake", 8, "DW")]
        vecs = [[1.0, 0.0], [0.0, 1.0]]
        result = merge_similar_clusters(reps, threshold=0.86,
                                        embed_fn=_fake_embed(vecs))
        assert len(result) == 2

    def test_prefers_non_reference_as_representative(self):
        """合并时非参考源应优先当代表（即便分数略低）。"""
        reps = [_rep("Quake hits Tokyo", 12, "Reuters", reference=True),
                _rep("Earthquake strikes Tokyo", 10, "BBC", reference=False)]
        vecs = [[1.0, 0.0], [1.0, 0.0]]
        result = merge_similar_clusters(reps, threshold=0.86,
                                        embed_fn=_fake_embed(vecs))
        assert len(result) == 1
        assert result[0]["source"] == "BBC", \
            f"非参考源应当选代表，实际 {result[0]['source']}"

    def test_merged_score_not_below_max(self):
        """合并后代表分不低于两簇原始最高分。"""
        reps = [_rep("a", 10, "BBC"), _rep("b", 8, "DW")]
        vecs = [[1.0, 0.0], [1.0, 0.0]]
        result = merge_similar_clusters(reps, threshold=0.86,
                                        embed_fn=_fake_embed(vecs))
        assert result[0]["score"] >= 10

    def test_result_sorted_by_score_desc(self):
        """结果按分数降序（三条互不相似，阈值拉高到 0.99）。"""
        reps = [_rep("low", 3, "BBC"), _rep("high", 15, "DW"),
                _rep("mid", 7, "CNBC")]
        vecs = [[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]]
        result = merge_similar_clusters(reps, threshold=0.99,
                                        embed_fn=_fake_embed(vecs))
        scores = [r["score"] for r in result]
        assert scores == sorted(scores, reverse=True)


class TestEmbedTitlesDegradation:
    def test_empty_titles_returns_none(self):
        assert embed_titles([]) is None
