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
# 标题→向量缓存：同一批标题在 scoring/linkage/finalcheck 多个阶段被反复 embed，
# 进程内缓存避免重复编码。run 短命、标题短，不设淘汰；进程退出即释放。
_VEC_CACHE: dict[str, list[float]] = {}


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

    # 只为未缓存的标题真正编码；全部命中缓存时连模型/库都不碰，直接拼装返回。
    missing = [t for t in titles if t not in _VEC_CACHE]
    if missing:
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
            # 去除 E5 特有的 "passage: " 前缀，MiniLM 直接传原文本即可
            raw = list(_MODEL.embed(missing))
            for t, v in zip(missing, raw):
                _VEC_CACHE[t] = _l2_normalize(v.tolist())
        except Exception as e:
            log.warning(f"嵌入编码失败，语义去重跳过：{type(e).__name__}: {e}")
            return None

    return [_VEC_CACHE[t] for t in titles]
