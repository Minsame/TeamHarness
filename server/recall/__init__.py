"""recall 领域包：召回服务（Agent 4）。

对应技术方案 3.2b 召回服务 + 7.5 故障降级，对外提供契约 API：
- RecallService.recall_list → POST /v1/recall/list
- RecallService.recall_read → POST /v1/recall/read
- RecallService.get_sync_status → GET /v1/sync/status

核心能力：
- 索引下钻（module_path 缩小候选集）+ 权限过滤（agent_binding JOIN asset_index status='active'）
- 向量 + BM25 + RRF 精排（EmbeddingService.fuse_rrf 复用）
- consistency: eventual | strict（strict 模式 git fetch 实时读）
- 响应体含 as_of_commit / sync_lag_seconds / degraded 标记
- DB 故障降级（内存 LRU + 模块 BM25，强制 module_path，未传 503）
- 离线降级（本地 git working copy 读取）
- recall/read 已删除资产 410 Gone + 替代建议
- OpenTelemetry 风格 trace_id 透传 + recall_log 写入
"""

from server.recall.service import (
    RecallItem,
    RecallListResponse,
    RecallReadResponse,
    RecallService,
    SyncStatusResponse,
    GoneResponse,
)
from server.recall.api import build_router, configure_recall, recall_router

__all__ = [
    "GoneResponse",
    "RecallItem",
    "RecallListResponse",
    "RecallReadResponse",
    "RecallService",
    "SyncStatusResponse",
    "build_router",
    "configure_recall",
    "recall_router",
]
