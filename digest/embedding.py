"""嵌入向量层：本地编码标题，供语义去重用。

为什么本地：DeepSeek API 没有 embedding 接口（纯对话补全），故用本地
fastembed（onnxruntime 后端，无 torch，体积小）。

设计铁律——任何失败都不许拖垮主流水线：
  · 未装 fastembed / 模型下载失败 / 编码异常 → 一律返回 None。
  · 调用方（merge_similar_clusters）见 None 即回退纯词面聚类。
语义去重是「可选增强」，不是「关键路径」，坏了就当它不存在。
"""

from __future__ import annotations

import logging

from .config import EMBED_MODEL

log = logging.getLogger(__name__)

# 模型加载一次后复用（首次会触发 onnx 模型下载到缓存目录）
_MODEL = None


def _l2_normalize(vec: list[float]) -> list[float]:
    """把向量缩放到单位长度，使余弦相似度 = 点积。零向量原样返回。"""
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0:
        return list(vec)
    return [x / norm for x in vec]


def embed_titles(titles: list[str]) -> list[list[float]] | None:
    """把标题列表编码成 L2 归一化的浮点向量（纯 Python list，便于无 numpy 下游）。

    返回 None 的三种情况：空输入 / 未装 fastembed / 编码出错——调用方据此降级。
    """
    if not titles:
        return None

    try:
        from fastembed import TextEmbedding
    except ImportError:
        log.info("未安装 fastembed，语义去重跳过（回退词面聚类）")
        return None

    try:
        global _MODEL
        if _MODEL is None:
            log.info(f"加载嵌入模型：{EMBED_MODEL}")
            _MODEL = TextEmbedding(model_name=EMBED_MODEL)
        # e5 系列约定：文档前缀 "passage: "。这里所有标题对称处理，统一前缀即可。
        raw = list(_MODEL.embed([f"passage: {t}" for t in titles]))
        return [_l2_normalize(v.tolist()) for v in raw]
    except Exception as e:
        log.warning(f"嵌入编码失败，语义去重跳过：{type(e).__name__}: {e}")
        return None
