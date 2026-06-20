"""跨期事件串联（#1）：把今天的代表作标成「近期事件的进展更新」。

思路：今天每条 rep 的英文原标题，与近 N 期 sidecar 里的英文原标题做向量余弦比对，
相似度 ≥ 阈值即判为「同一事件的进展」，原地在 rep 上写 progress_of 标记。

铁律——质量增强绝不阻断主流程：
  · 总开关关 / embed 不可用（返回 None）/ 无历史 sidecar → 直接 return，不报错。
  · 任何异常一律 log.warning 后吞掉，绝不向上抛、绝不拖垮成稿与推送。
向量来自 embedding.embed_titles：纯 Python list、已 L2 归一化，故余弦 = 点积。
"""

from __future__ import annotations

import logging

from .config import PROGRESS_LINK_ENABLED, PROGRESS_SIM_THRESHOLD
from .embedding import embed_titles
from .storage import load_recent_reps

log = logging.getLogger(__name__)


def _cosine(v1: list[float], v2: list[float]) -> float:
    """两个 L2 归一化向量的余弦相似度 = 点积（纯 Python，无 numpy）。"""
    return sum(a * b for a, b in zip(v1, v2))


def tag_progress(reps: list[dict]) -> None:
    """对每条 today rep，与近 N 期 sidecar 的 raw_title 向量比对；命中即原地写 rep['progress_of']。

    embed 不可用 / 无历史 → 直接 return（降级），绝不抛错。
    """
    if not PROGRESS_LINK_ENABLED:
        return
    if not reps:
        return

    try:
        past = load_recent_reps()
        if not past:
            return

        today_vecs = embed_titles([r["title"] for r in reps])
        past_vecs = embed_titles([p["raw_title"] for p in past])
        if today_vecs is None or past_vecs is None:
            return

        for i, vec in enumerate(today_vecs):
            best_sim = -1.0
            best_j = -1
            for j, pvec in enumerate(past_vecs):
                sim = _cosine(vec, pvec)
                if sim > best_sim:
                    best_sim = sim
                    best_j = j

            if best_j >= 0 and best_sim >= PROGRESS_SIM_THRESHOLD:
                prev = past[best_j]
                reps[i]["progress_of"] = {
                    "prev_zh": prev.get("zh") or prev["raw_title"],
                    "date": prev["date"],
                }
                log.info(
                    f"跨期串联命中：rep[{i}] 是 {prev['date']} 事件的进展"
                    f"（sim={best_sim:.3f}）"
                )
    except Exception as e:
        log.warning(f"跨期事件串联失败，跳过（不影响成稿）: {type(e).__name__}: {e}")
        return
