"""成员 AI 通信模块（服务端）。

提供 /v1/comm/* 端点，作为 central 拓扑下的消息中转与状态查询层。
对应 spec `.trae/specs/add-shadow-comm-module/spec.md` 中设计但未实现的服务端端点。

端点：
- GET  /v1/comm/peers              列出 peer（含在线状态 + 标签）
- POST /v1/comm/heartbeat          前端定期心跳（更新在线状态）
- POST /v1/comm/ask                发起询问（记录 ask 事件，等待 peer 回复）
- POST /v1/comm/answer             回复询问（记录 answer 事件，更新对账状态）
- GET  /v1/comm/conversations      对话历史查询（按 peer/type/方向过滤）
- GET  /v1/comm/shadow-log         影子对账状态（degraded=true 的事件）
- GET  /v1/comm/outbox             outbox 待投递消息
- GET  /v1/comm/conversations/{event_id}/thread  对话线程（回复链展开）

数据表：
- comm_events：交流事件（ask/answer/confirmed/revised/needs_human_review）
- comm_peer_status：peer 在线状态（心跳维护）

与客户端的关系：
- 客户端 PeerComm 通过 transport 调用 /v1/comm/ask / /v1/comm/answer
- 客户端 daemon 定期心跳 /v1/comm/heartbeat
- 前端直接调用 /v1/comm/* 查看与发送
"""

from server.comm.api import comm_router, configure_comm_api

__all__ = ["comm_router", "configure_comm_api"]
