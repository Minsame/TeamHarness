// API 服务层 — 封装所有后端调用
// 全局暴露，供各页面模块使用

window.TeamHarnessAPI = (function () {
    const API_BASE = "";

    async function request(path, options = {}) {
        const apiKey = localStorage.getItem("teamharness_api_key") || "";
        const headers = {
            "Content-Type": "application/json",
            ...(apiKey ? { "X-API-Key": apiKey } : {}),
            ...options.headers,
        };

        // 超时控制：15s 后 abort
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 15000);

        let resp;
        try {
            resp = await fetch(API_BASE + path, {
                ...options,
                headers,
                signal: controller.signal,
            });
        } catch (err) {
            clearTimeout(timeoutId);
            if (err.name === "AbortError") {
                throw new Error("请求超时，请稍后重试");
            }
            throw err;
        }
        clearTimeout(timeoutId);

        // 401 拦截：清除 localStorage，引导回登录页
        if (resp.status === 401) {
            localStorage.removeItem("teamharness_api_key");
            localStorage.removeItem("teamharness_member_id");
            const ElMsg = window.ElementPlus?.ElMessage;
            if (ElMsg) ElMsg.error("登录已过期，请重新登录");
            setTimeout(() => window.location.reload(), 1500);
            throw new Error("登录已过期，请重新登录");
        }

        if (!resp.ok) {
            let detail = `${resp.status} ${resp.statusText}`;
            try {
                const body = await resp.json();
                detail = body.detail || detail;
            } catch (_) {}
            throw new Error(detail);
        }
        return resp.json();
    }

    return {
        // 认证
        issueApiKey: (memberId, agentId) =>
            request("/v1/auth/apikey", {
                method: "POST",
                body: JSON.stringify({ member_id: memberId, agent_id: agentId }),
            }),
        lookupApiKey: (apiKey) =>
            request("/v1/auth/apikey/lookup", {
                method: "POST",
                body: JSON.stringify({ api_key: apiKey }),
            }),

        // 资产 CRUD
        listAssets: (params) => {
            const qs = new URLSearchParams(params).toString();
            return request(`/v1/assets?${qs}`);
        },
        getAsset: (id) => request(`/v1/assets/${id}`),
        updateAssetScope: (id, scope) =>
            request(`/v1/assets/${id}/scope`, {
                method: "PATCH",
                body: JSON.stringify({ scope }),
            }),

        // 成员统计
        getMemberStats: (memberId) => request(`/v1/members/${memberId}/stats`),

        // 资产图谱
        getAssetLinks: (id) => request(`/v1/assets/${id}/links`),
        createAssetLink: (id, dstId, linkType) =>
            request(`/v1/assets/${id}/links`, {
                method: "POST",
                body: JSON.stringify({ dst_asset_id: dstId, link_type: linkType }),
            }),
        deleteAssetLink: (assetId, linkId) =>
            request(`/v1/assets/${assetId}/links/${linkId}`, { method: "DELETE" }),
        getAssetGraph: (id, depth = 2) => request(`/v1/assets/${id}/graph?depth=${depth}`),

        // ACL
        getAssetAcl: (id) => request(`/v1/assets/${id}/acl`),
        createAssetAcl: (id, granteeType, granteeId, permission, grantedBy) =>
            request(`/v1/assets/${id}/acl`, {
                method: "POST",
                body: JSON.stringify({
                    grantee_type: granteeType,
                    grantee_id: granteeId,
                    permission,
                    granted_by: grantedBy,
                }),
            }),
        deleteAssetAcl: (assetId, aclId) =>
            request(`/v1/assets/${assetId}/acl/${aclId}`, { method: "DELETE" }),

        // 治理看板
        getGovernanceDashboard: () => request("/v1/governance/dashboard"),

        // 团队管理
        listMembers: () => request("/v1/team/members"),
        getMember: (memberId) => request(`/v1/team/members/${memberId}`),
        listTags: () => request("/v1/team/tags"),
        addMember: (memberId, displayName, role, tags) =>
            request("/v1/team/members", {
                method: "POST",
                body: JSON.stringify({ member_id: memberId, display_name: displayName, role: role || "member", tags }),
            }),
        updateMember: (memberId, updates) =>
            request(`/v1/team/members/${memberId}`, {
                method: "PATCH",
                body: JSON.stringify(updates),
            }),
        deleteMember: (memberId) =>
            request(`/v1/team/members/${memberId}`, { method: "DELETE" }),

        getTeamTree: () => request("/v1/team/teams/tree"),
        createTeam: (name, description) =>
            request("/v1/team/teams", {
                method: "POST",
                body: JSON.stringify({ name, description: description || "" }),
            }),
        createSubTeam: (parentId, name, description) =>
            request(`/v1/team/teams/${parentId}/sub`, {
                method: "POST",
                body: JSON.stringify({ name, description: description || "" }),
            }),
        updateTeam: (teamId, updates) =>
            request(`/v1/team/teams/${teamId}`, {
                method: "PATCH",
                body: JSON.stringify(updates),
            }),
        deleteTeam: (teamId) =>
            request(`/v1/team/teams/${teamId}`, { method: "DELETE" }),

        getTeamMembers: (teamId) => request(`/v1/team/teams/${teamId}/members`),
        addTeamMember: (teamId, memberId, role) =>
            request(`/v1/team/teams/${teamId}/members`, {
                method: "POST",
                body: JSON.stringify({ member_id: memberId, role: role || "member" }),
            }),
        removeTeamMember: (teamId, memberId) =>
            request(`/v1/team/teams/${teamId}/members/${memberId}`, { method: "DELETE" }),

        // ===== 影子通信（comm 域） =====
        // Peer 列表 + 在线状态
        listPeers: () => request("/v1/comm/peers"),
        heartbeat: (endpoint = "") =>
            request("/v1/comm/heartbeat", {
                method: "POST",
                body: JSON.stringify({ endpoint }),
            }),

        // 发送消息
        askPeer: (toPeer, question, inReplyTo = "") =>
            request("/v1/comm/ask", {
                method: "POST",
                body: JSON.stringify({ to_peer: toPeer, question, in_reply_to: inReplyTo }),
            }),
        answerPeer: (eventId, answer, realtime, basedOn = "", snapshotStale = false) =>
            request("/v1/comm/answer", {
                method: "POST",
                body: JSON.stringify({
                    event_id: eventId,
                    answer,
                    realtime,
                    based_on: basedOn,
                    snapshot_stale: snapshotStale,
                }),
            }),
        reconcileAnswer: (eventId, verdict, revisedAnswer = "") =>
            request("/v1/comm/reconcile", {
                method: "POST",
                body: JSON.stringify({ event_id: eventId, verdict, revised_answer: revisedAnswer }),
            }),

        // 对话历史
        listConversations: (params = {}) => {
            const qs = new URLSearchParams(params).toString();
            return request(`/v1/comm/conversations${qs ? "?" + qs : ""}`);
        },
        getThread: (eventId) => request(`/v1/comm/conversations/${eventId}/thread`),

        // 影子对账
        listShadowLog: (params = {}) => {
            const qs = new URLSearchParams(params).toString();
            return request(`/v1/comm/shadow-log${qs ? "?" + qs : ""}`);
        },
        listOutbox: (params = {}) => {
            const qs = new URLSearchParams(params).toString();
            return request(`/v1/comm/outbox${qs ? "?" + qs : ""}`);
        },
    };
})();
