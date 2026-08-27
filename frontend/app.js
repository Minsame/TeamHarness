// TeamHarness 规则库管理前端应用
// Vue 3 + Element Plus（CDN 引入，无需构建）
// 依赖：services/api.js + services/utils.js（需在 app.js 之前加载）

const { createApp, ref, reactive, computed, onMounted, onUnmounted, nextTick } = Vue;
const { ElMessage, ElMessageBox } = ElementPlus;
const { Refresh } = ElementPlusIconsVue;
const API = window.TeamHarnessAPI;
const Utils = window.TeamHarnessUtils;

const app = createApp({
    setup() {
        // ---------------- 登录 ----------------
        const loggedIn = ref(false);
        const currentMember = ref("");
        const currentRole = ref("member");  // admin / member
        const loginLoading = ref(false);
        const loginForm = reactive({ memberId: "", apiKey: "" });

        const showIssueDialog = ref(false);
        const issueLoading = ref(false);
        const issueForm = reactive({ memberId: "", agentId: "", tags: [] });

        async function checkLogin() {
            const key = localStorage.getItem("teamharness_api_key");
            const member = localStorage.getItem("teamharness_member_id");
            if (key && member) {
                // 校验 key 有效性（防止 localStorage 被篡改为无效值）
                try {
                    const resp = await API.lookupApiKey(key);
                    if (!resp || !resp.agent_id) {
                        // key 无效，清除并回到登录页
                        localStorage.removeItem("teamharness_api_key");
                        localStorage.removeItem("teamharness_member_id");
                        loggedIn.value = false;
                        return false;
                    }
                    currentMember.value = member;
                    loggedIn.value = true;
                    fetchCurrentRole();
                    return true;
                } catch (_) {
                    // lookup 失败（401/网络错误），不阻塞登录（可能后端暂时不可用）
                    currentMember.value = member;
                    loggedIn.value = true;
                    fetchCurrentRole();
                    return true;
                }
            }
            return false;
        }

        async function fetchCurrentRole() {
            try {
                const info = await API.getMember(currentMember.value);
                currentRole.value = info?.role === "admin" ? "admin" : "member";
            } catch (_) {
                currentRole.value = "member";
            }
        }

        async function handleLogin() {
            if (!loginForm.memberId || !loginForm.apiKey) {
                ElMessage.warning("请输入成员 ID 和 API Key");
                return;
            }
            loginLoading.value = true;
            try {
                const resp = await API.lookupApiKey(loginForm.apiKey);
                // 后端对无效 key 返回 200 + agent_id=null（反查接口设计意图）
                // 前端必须校验 agent_id 非空，否则无效 key 也能登录
                if (!resp || !resp.agent_id) {
                    ElMessage.error("API Key 无效或已失效");
                    return;
                }
                localStorage.setItem("teamharness_api_key", loginForm.apiKey);
                localStorage.setItem("teamharness_member_id", loginForm.memberId);
                currentMember.value = loginForm.memberId;
                loggedIn.value = true;
                fetchCurrentRole();
                ElMessage.success("登录成功");
                onMountedInit();
            } catch (err) {
                ElMessage.error("登录失败：" + err.message);
            } finally {
                loginLoading.value = false;
            }
        }

        function logout() {
            localStorage.removeItem("teamharness_api_key");
            localStorage.removeItem("teamharness_member_id");
            loggedIn.value = false;
            currentMember.value = "";
            currentRole.value = "member";
            loginForm.apiKey = "";
        }

        async function handleIssueKey() {
            if (!issueForm.memberId) {
                ElMessage.warning("请输入成员 ID");
                return;
            }
            if (!issueForm.tags || issueForm.tags.length === 0) {
                ElMessage.warning("请至少选择一个成员标签");
                return;
            }
            issueLoading.value = true;
            try {
                // 颁发 key 时同时注册成员（含标签）
                const resp = await API.issueApiKey(issueForm.memberId, issueForm.agentId || undefined);
                // 颁发成功后，如果 members 表还没有该成员，自动创建
                try {
                    await API.addMember(issueForm.memberId, issueForm.memberId, "member", issueForm.tags);
                } catch (e) {
                    // 成员可能已存在（409），忽略
                }
                ElMessageBox.alert(
                    `API Key: <code>${resp.api_key}</code><br><br>请妥善保存，此 Key 仅显示一次。`,
                    "颁发成功",
                    { dangerouslyUseHTMLString: true, type: "success" }
                );
                showIssueDialog.value = false;
                issueForm.memberId = "";
                issueForm.agentId = "";
                issueForm.tags = [];
            } catch (err) {
                ElMessage.error("颁发失败：" + err.message);
            } finally {
                issueLoading.value = false;
            }
        }

        // ---------------- 菜单 ----------------
        const activeMenu = ref("my");

        function handleMenuSelect(index) {
            // 离开 comm 页面时停止心跳
            if (activeMenu.value === "comm" && index !== "comm") {
                stopCommHeartbeat();
            }
            activeMenu.value = index;
            if (window.history.state?.menu !== index) {
                window.history.pushState({ menu: index }, "", `#${index}`);
            }
            if (index === "my") loadMyAssets();
            else if (index === "shared") loadSharedAssets();
            else if (index === "share-mgmt") loadShareMgmtAssets();
            else if (index === "graph") loadGraphRoot();
            else if (index === "acl") loadAclAssets();
            else if (index === "dashboard") loadMemberStats();
            else if (index === "comm") loadPeers();
            else if (index === "team") loadTeamTree();
            else if (index === "members") loadMembers();
        }

        function onMountedInit() {
            loadMyAssets();
            loadMemberStats();
        }

        // ---------------- 我的规则库 ----------------
        const myAssets = ref([]);
        const myLoading = ref(false);
        const myFilter = reactive({ type: "", scope: "", category: "", modulePath: "" });
        const myPage = reactive({ page: 1, size: 20, total: 0 });

        async function loadMyAssets() {
            myLoading.value = true;
            try {
                const params = { owner: currentMember.value, limit: myPage.size, offset: (myPage.page - 1) * myPage.size };
                if (myFilter.type) params.type = myFilter.type;
                if (myFilter.scope) params.scope = myFilter.scope;
                if (myFilter.category) params.category = myFilter.category;
                if (myFilter.modulePath) params.module_path = myFilter.modulePath;
                const resp = await API.listAssets(params);
                myAssets.value = resp.items;
                myPage.total = resp.total;
            } catch (err) {
                ElMessage.error("加载失败：" + err.message);
            } finally {
                myLoading.value = false;
            }
        }

        // ---------------- 共享库 ----------------
        const sharedAssets = ref([]);
        const sharedLoading = ref(false);
        const sharedFilter = reactive({ scope: "team", type: "", owner: "", category: "" });
        const sharedPage = reactive({ page: 1, size: 20, total: 0 });

        async function loadSharedAssets() {
            sharedLoading.value = true;
            try {
                const params = { limit: sharedPage.size, offset: (sharedPage.page - 1) * sharedPage.size };
                if (sharedFilter.scope) params.scope = sharedFilter.scope;
                if (sharedFilter.type) params.type = sharedFilter.type;
                if (sharedFilter.owner) params.owner = sharedFilter.owner;
                if (sharedFilter.category) params.category = sharedFilter.category;
                const resp = await API.listAssets(params);
                sharedAssets.value = resp.items;
                sharedPage.total = resp.total;
            } catch (err) {
                ElMessage.error("加载失败：" + err.message);
            } finally {
                sharedLoading.value = false;
            }
        }

        // ---------------- 共享管理 ----------------
        const shareMgmtAssets = ref([]);
        const shareMgmtLoading = ref(false);
        const shareMgmtFilter = reactive({ scope: "" });
        const shareMgmtSelection = ref([]);

        async function loadShareMgmtAssets() {
            shareMgmtLoading.value = true;
            try {
                const params = { owner: currentMember.value, limit: 200, offset: 0 };
                if (shareMgmtFilter.scope) params.scope = shareMgmtFilter.scope;
                const resp = await API.listAssets(params);
                shareMgmtAssets.value = resp.items;
            } catch (err) {
                ElMessage.error("加载失败：" + err.message);
            } finally {
                shareMgmtLoading.value = false;
            }
        }

        function handleShareMgmtSelectionChange(val) {
            shareMgmtSelection.value = val;
        }

        const batchScopeDialogVisible = ref(false);
        const batchScopeNewValue = ref("team");
        const batchScopeUpdating = ref(false);

        function showBatchScopeDialog() {
            if (shareMgmtSelection.value.length === 0) {
                ElMessage.warning("请先勾选要修改的资产");
                return;
            }
            batchScopeNewValue.value = "team";
            batchScopeDialogVisible.value = true;
        }

        async function handleBatchUpdateScope() {
            batchScopeUpdating.value = true;
            try {
                let ok = 0, fail = 0;
                for (const asset of shareMgmtSelection.value) {
                    try {
                        await API.updateAssetScope(asset.id, batchScopeNewValue.value);
                        ok++;
                    } catch (_) {
                        fail++;
                    }
                }
                ElMessage.success(`批量修改完成：成功 ${ok}，失败 ${fail}`);
                batchScopeDialogVisible.value = false;
                loadShareMgmtAssets();
            } catch (err) {
                ElMessage.error("批量修改失败：" + err.message);
            } finally {
                batchScopeUpdating.value = false;
            }
        }

        async function quickChangeScope(row, newScope) {
            if (newScope === row.scope) return;
            try {
                await ElMessageBox.confirm(
                    `确认将资产 ${row.id} 的共享范围从 ${Utils.scopeLabel(row.scope)} 修改为 ${Utils.scopeLabel(newScope)}？`,
                    "确认修改",
                    { type: "warning" }
                );
                await API.updateAssetScope(row.id, newScope);
                ElMessage.success("修改成功");
                loadShareMgmtAssets();
            } catch (err) {
                if (err !== "cancel" && err.message) {
                    ElMessage.error("修改失败：" + err.message);
                }
                loadShareMgmtAssets();
            }
        }

        // ---------------- 资产详情 ----------------
        const detailDialogVisible = ref(false);
        const detailData = ref(null);
        const detailLoading = ref(false);

        async function showDetail(row) {
            detailDialogVisible.value = true;
            detailLoading.value = true;
            detailData.value = null;
            try {
                detailData.value = await API.getAsset(row.id);
            } catch (err) {
                ElMessage.error("加载详情失败：" + err.message);
                detailDialogVisible.value = false;
            } finally {
                detailLoading.value = false;
            }
        }

        // ---------------- 单个共享修改 ----------------
        const scopeDialogVisible = ref(false);
        const scopeDialogData = ref(null);
        const scopeDialogNewValue = ref("team");
        const scopeUpdating = ref(false);

        function showScopeDialog(row) {
            scopeDialogData.value = row;
            scopeDialogNewValue.value = row.scope;
            scopeDialogVisible.value = true;
        }

        async function handleUpdateScope() {
            if (!scopeDialogData.value) return;
            if (scopeUpdating.value) return;  // 防重入守卫
            scopeUpdating.value = true;
            try {
                await API.updateAssetScope(scopeDialogData.value.id, scopeDialogNewValue.value);
                ElMessage.success("共享范围修改成功");
                scopeDialogVisible.value = false;
                loadMyAssets();
                loadMemberStats();
            } catch (err) {
                ElMessage.error("修改失败：" + err.message);
            } finally {
                scopeUpdating.value = false;
            }
        }

        // ---------------- 资产图谱 ----------------
        const graphRootId = ref("");
        const graphDepth = ref(2);
        const graphData = ref(null);
        const graphLoading = ref(false);
        const linkDialogVisible = ref(false);
        const linkForm = reactive({ dstAssetId: "", linkType: "related_to" });
        const linkLoading = ref(false);
        const currentLinksAsset = ref(null);
        const linksDialogVisible = ref(false);
        const linksData = ref(null);
        const linksLoading = ref(false);

        async function loadGraphRoot() {
            if (!graphRootId.value && myAssets.value.length > 0) {
                graphRootId.value = myAssets.value[0].id;
            }
        }

        async function loadGraph() {
            if (!graphRootId.value) {
                ElMessage.warning("请输入资产 ID");
                return;
            }
            graphLoading.value = true;
            try {
                graphData.value = await API.getAssetGraph(graphRootId.value, graphDepth.value);
            } catch (err) {
                ElMessage.error("加载图谱失败：" + err.message);
            } finally {
                graphLoading.value = false;
            }
        }

        async function showLinksDialog(row) {
            currentLinksAsset.value = row;
            linksDialogVisible.value = true;
            linksLoading.value = true;
            linksData.value = null;
            try {
                linksData.value = await API.getAssetLinks(row.id);
            } catch (err) {
                ElMessage.error("加载关联失败：" + err.message);
            } finally {
                linksLoading.value = false;
            }
        }

        function showAddLinkDialog() {
            linkForm.dstAssetId = "";
            linkForm.linkType = "related_to";
            linkDialogVisible.value = true;
        }

        async function handleAddLink() {
            if (!linkForm.dstAssetId) {
                ElMessage.warning("请输入目标资产 ID");
                return;
            }
            linkLoading.value = true;
            try {
                await API.createAssetLink(currentLinksAsset.value.id, linkForm.dstAssetId, linkForm.linkType);
                ElMessage.success("关联已添加");
                linkDialogVisible.value = false;
                linksData.value = await API.getAssetLinks(currentLinksAsset.value.id);
            } catch (err) {
                ElMessage.error("添加失败：" + err.message);
            } finally {
                linkLoading.value = false;
            }
        }

        async function handleDeleteLink(linkId) {
            try {
                await ElMessageBox.confirm("确认删除此关联？", "确认", { type: "warning" });
                await API.deleteAssetLink(currentLinksAsset.value.id, linkId);
                ElMessage.success("关联已删除");
                linksData.value = await API.getAssetLinks(currentLinksAsset.value.id);
            } catch (err) {
                if (err !== "cancel" && err.message) ElMessage.error("删除失败：" + err.message);
            }
        }

        // ---------------- ACL 管理 ----------------
        const aclAssets = ref([]);
        const aclLoading = ref(false);
        const aclDialogVisible = ref(false);
        const aclData = ref(null);
        const aclLoading2 = ref(false);
        const currentAclAsset = ref(null);
        const aclFormDialogVisible = ref(false);
        const aclForm = reactive({ granteeType: "user", granteeId: "", permission: "read" });
        const aclFormLoading = ref(false);

        async function loadAclAssets() {
            aclLoading.value = true;
            try {
                const resp = await API.listAssets({ owner: currentMember.value, scope: "restricted", limit: 200 });
                aclAssets.value = resp.items;
            } catch (err) {
                ElMessage.error("加载失败：" + err.message);
            } finally {
                aclLoading.value = false;
            }
        }

        async function showAclDialog(row) {
            currentAclAsset.value = row;
            aclDialogVisible.value = true;
            aclLoading2.value = true;
            aclData.value = null;
            try {
                aclData.value = await API.getAssetAcl(row.id);
            } catch (err) {
                ElMessage.error("加载 ACL 失败：" + err.message);
            } finally {
                aclLoading2.value = false;
            }
        }

        function showAddAclDialog() {
            aclForm.granteeType = "user";
            aclForm.granteeId = "";
            aclForm.permission = "read";
            aclFormDialogVisible.value = true;
        }

        async function handleAddAcl() {
            if (!aclForm.granteeId) {
                ElMessage.warning("请输入授权对象 ID");
                return;
            }
            aclFormLoading.value = true;
            try {
                await API.createAssetAcl(
                    currentAclAsset.value.id,
                    aclForm.granteeType,
                    aclForm.granteeId,
                    aclForm.permission,
                    currentMember.value
                );
                ElMessage.success("授权已添加");
                aclFormDialogVisible.value = false;
                aclData.value = await API.getAssetAcl(currentAclAsset.value.id);
            } catch (err) {
                ElMessage.error("添加失败：" + err.message);
            } finally {
                aclFormLoading.value = false;
            }
        }

        async function handleDeleteAcl(aclId) {
            try {
                await ElMessageBox.confirm("确认撤销此授权？", "确认", { type: "warning" });
                await API.deleteAssetAcl(currentAclAsset.value.id, aclId);
                ElMessage.success("授权已撤销");
                aclData.value = await API.getAssetAcl(currentAclAsset.value.id);
            } catch (err) {
                if (err !== "cancel" && err.message) ElMessage.error("撤销失败：" + err.message);
            }
        }

        // ---------------- 成员统计 ----------------
        const memberStats = reactive({ total: 0, by_type: {}, by_scope: {}, by_status: {}, by_module: {} });

        async function loadMemberStats() {
            try {
                const resp = await API.getMemberStats(currentMember.value);
                Object.assign(memberStats, resp);
            } catch (err) {
                console.error("加载统计失败:", err);
            }
        }

        // ---------------- 影子通信 ----------------
        const commTab = ref("peers");
        const peersList = ref([]);
        const peersLoading = ref(false);
        const commHeartbeatRunning = ref(false);
        let commHeartbeatTimer = null;

        const onlinePeersCount = computed(() => peersList.value.filter(p => p.online).length);

        async function loadPeers() {
            peersLoading.value = true;
            try {
                peersList.value = await API.listPeers();
                // 首次加载时启动心跳（维护自身在线状态）
                startCommHeartbeat();
            } catch (err) {
                ElMessage.error("加载 Peer 列表失败：" + err.message);
            } finally {
                peersLoading.value = false;
            }
        }

        async function sendHeartbeatOnce() {
            try {
                await API.heartbeat("");
            } catch (err) {
                // 心跳失败不弹窗，仅 console
                console.warn("心跳失败:", err.message);
            }
        }

        function startCommHeartbeat() {
            if (commHeartbeatTimer) return;
            // 立即发一次心跳，然后每 60s 一次（后端超时 120s）
            sendHeartbeatOnce();
            commHeartbeatTimer = setInterval(sendHeartbeatOnce, 60000);
            commHeartbeatRunning.value = true;
        }

        function stopCommHeartbeat() {
            if (commHeartbeatTimer) {
                clearInterval(commHeartbeatTimer);
                commHeartbeatTimer = null;
            }
            commHeartbeatRunning.value = false;
        }

        function handleCommTabChange(tabName) {
            if (tabName === "peers") loadPeers();
            else if (tabName === "conversations") loadConversations();
            else if (tabName === "shadow") loadShadowLog();
            else if (tabName === "ask" && askForm.toPeer) loadChatHistory();
        }

        // 对话历史
        const convList = ref([]);
        const convLoading = ref(false);
        const convFilter = reactive({ peer: "", type: "", direction: "" });

        async function loadConversations() {
            convLoading.value = true;
            try {
                const params = { limit: 50, offset: 0 };
                if (convFilter.peer) params.peer = convFilter.peer;
                if (convFilter.type) params.type = convFilter.type;
                if (convFilter.direction) params.direction = convFilter.direction;
                const resp = await API.listConversations(params);
                convList.value = resp.items || [];
            } catch (err) {
                ElMessage.error("加载对话历史失败：" + err.message);
            } finally {
                convLoading.value = false;
            }
        }

        function showPeerConversations(peer) {
            convFilter.peer = peer.member_id;
            convFilter.type = "";
            convFilter.direction = "";
            commTab.value = "conversations";
            loadConversations();
        }

        // 即时对话（聊天式）
        const askForm = reactive({ toPeer: "", question: "", inReplyTo: "" });
        const askLoading = ref(false);
        const chatMessages = ref([]);
        const chatBoxRef = ref(null);

        function showAskFromPeer(peer) {
            askForm.toPeer = peer.member_id;
            askForm.question = "";
            askForm.inReplyTo = "";
            commTab.value = "ask";
            loadChatHistory();
        }

        function clearAskForm() {
            askForm.question = "";
            askForm.inReplyTo = "";
        }

        function peerOnline(memberId) {
            const p = peersList.value.find(x => x.member_id === memberId);
            return p ? p.online : false;
        }

        function chatMessageText(msg) {
            // ask 事件 payload.question；answer 事件 payload.answer
            const p = msg.payload || {};
            if (p.question) return p.question;
            if (p.answer) return p.answer;
            if (p.revised_answer) return `[修订] ${p.revised_answer}`;
            return JSON.stringify(p);
        }

        function formatChatTime(ts) {
            if (!ts) return "";
            const d = new Date(ts);
            const now = new Date();
            const sameDay = d.toDateString() === now.toDateString();
            const hh = String(d.getHours()).padStart(2, "0");
            const mm = String(d.getMinutes()).padStart(2, "0");
            if (sameDay) return `${hh}:${mm}`;
            const MM = String(d.getMonth() + 1).padStart(2, "0");
            const dd = String(d.getDate()).padStart(2, "0");
            return `${MM}-${dd} ${hh}:${mm}`;
        }

        async function loadChatHistory() {
            if (!askForm.toPeer) {
                chatMessages.value = [];
                return;
            }
            try {
                const resp = await API.listConversations({ peer: askForm.toPeer, limit: 100, offset: 0 });
                chatMessages.value = resp.items || [];
                // 滚动到底部
                await nextTick();
                if (chatBoxRef.value) chatBoxRef.value.scrollTop = chatBoxRef.value.scrollHeight;
            } catch (err) {
                ElMessage.error("加载对话记录失败：" + err.message);
            }
        }

        async function handleAskPeer() {
            if (!askForm.toPeer) {
                ElMessage.warning("请选择对话对象");
                return;
            }
            if (!askForm.question.trim()) {
                ElMessage.warning("请输入消息内容");
                return;
            }
            if (askLoading.value) return;  // R045: 重入守卫
            askLoading.value = true;
            try {
                // 判断当前角色：如果消息流最后一条是对方发来的 ask，我是接收方 → 回复
                // 否则我是发起方 → 发起新 ask
                const lastMsg = chatMessages.value[chatMessages.value.length - 1];
                const isReplying = lastMsg
                    && lastMsg.from_member !== currentMember.value  // 对方发的
                    && lastMsg.event_type === "ask";                 // 是 ask（待回复）
                const peerIsOnline = peerOnline(askForm.toPeer);
                let resp;
                if (isReplying) {
                    // 回复对方的 ask：调 answerPeer，realtime 取决于 peer 是否在线
                    resp = await API.answerPeer(
                        lastMsg.event_id,
                        askForm.question,
                        peerIsOnline,  // realtime
                    );
                    ElMessage.success(peerIsOnline ? "回复已实时投递" : "对方离线，回复已记录为影子模拟回答");
                } else {
                    // 发起新 ask
                    resp = await API.askPeer(askForm.toPeer, askForm.question, askForm.inReplyTo);
                    ElMessage.success(!resp.degraded ? "消息已实时投递" : "对方离线，已进入影子联络（待投递）");
                }
                askForm.inReplyTo = "";
                clearAskForm();
                await loadChatHistory();  // 刷新消息流
                loadPeers();  // 刷新 peer 在线状态
            } catch (err) {
                ElMessage.error("发送失败：" + err.message);
            } finally {
                askLoading.value = false;
            }
        }

        // 影子对账
        const shadowList = ref([]);
        const shadowLoading = ref(false);
        const shadowFilter = reactive({ status: "" });

        async function loadShadowLog() {
            shadowLoading.value = true;
            try {
                const params = { limit: 50, offset: 0 };
                if (shadowFilter.status) params.status = shadowFilter.status;
                const resp = await API.listShadowLog(params);
                shadowList.value = resp.items || [];
            } catch (err) {
                ElMessage.error("加载影子对账失败：" + err.message);
            } finally {
                shadowLoading.value = false;
            }
        }

        async function handleReconcile(row, verdict) {
            const verdictLabel = { confirmed: "确认", revised: "修订", needs_human_review: "转人工审核" }[verdict];
            try {
                let revisedAnswer = "";
                if (verdict === "revised") {
                    // 修订需要输入新的回答内容
                    const result = await ElMessageBox.prompt("请输入修订后的回答内容", "修订回答", {
                        confirmButtonText: "确定",
                        cancelButtonText: "取消",
                        inputType: "textarea",
                        inputPlaceholder: "修订后的回答",
                    });
                    revisedAnswer = result.value || "";
                    if (!revisedAnswer.trim()) {
                        ElMessage.warning("修订回答不能为空");
                        return;
                    }
                } else {
                    await ElMessageBox.confirm(
                        `确认将事件 ${row.event_id} 标记为「${verdictLabel}」？`,
                        "确认对账",
                        { type: "warning" }
                    );
                }
                await API.reconcileAnswer(row.event_id, verdict, revisedAnswer);
                ElMessage.success(`已标记为「${verdictLabel}」`);
                loadShadowLog();
            } catch (err) {
                if (err === "cancel") return;
                ElMessage.error("对账失败：" + err.message);
            }
        }

        // 对话线程查看器
        const threadDialogVisible = ref(false);
        const threadData = ref(null);
        const threadLoading = ref(false);

        async function showThreadDialog(row) {
            threadDialogVisible.value = true;
            threadLoading.value = true;
            threadData.value = null;
            try {
                threadData.value = await API.getThread(row.event_id);
            } catch (err) {
                ElMessage.error("加载线程失败：" + err.message);
                threadDialogVisible.value = false;
            } finally {
                threadLoading.value = false;
            }
        }

        // 事件类型 / 状态 → tag type
        function eventTypeTag(type) {
            const map = {
                ask: "primary",
                realtime_answer: "success",
                simulated_answer: "warning",
                confirmed: "success",
                revised: "warning",
                needs_human_review: "danger",
            };
            return map[type] || "info";
        }

        function statusTagType(status) {
            const map = {
                pending_delivery: "info",
                delivered: "",
                confirmed: "success",
                revised: "warning",
                needs_human_review: "danger",
            };
            return map[status] || "info";
        }

        // ---------------- 团队管理 ----------------
        const teamTree = ref([]);
        const teamLoading = ref(false);
        const teamDialogVisible = ref(false);
        const teamForm = reactive({ name: "", description: "", parentId: null });
        const teamDialogMode = ref("create"); // create / sub
        const memberList = ref([]);
        const memberLoading = ref(false);
        const memberDialogVisible = ref(false);
        const memberForm = reactive({ memberId: "", displayName: "", role: "member", tags: [] });
        const tagSuggestions = ref(["前端", "后端", "全栈", "测试", "运维", "DBA", "产品", "设计"]);
        const editTagsDialogVisible = ref(false);
        const editTagsForm = reactive({ memberId: "", tags: [] });
        const addTeamMemberDialogVisible = ref(false);
        const addTeamMemberForm = reactive({ memberId: "", role: "member" });
        const currentTeam = ref(null);
        const teamMembersDialogVisible = ref(false);
        const teamMembersList = ref([]);
        const teamMembersLoading = ref(false);

        async function loadTeamTree() {
            teamLoading.value = true;
            try {
                teamTree.value = await API.getTeamTree();
            } catch (err) {
                // 403 = 非 admin，显示提示
                if (err.message.includes("403") || err.message.includes("无权")) {
                    ElMessage.warning("需要管理员权限才能查看团队管理");
                } else {
                    ElMessage.error("加载团队失败：" + err.message);
                }
                teamTree.value = [];
            } finally {
                teamLoading.value = false;
            }
        }

        function showCreateTeamDialog() {
            teamDialogMode.value = "create";
            teamForm.name = "";
            teamForm.description = "";
            teamForm.parentId = null;
            teamDialogVisible.value = true;
        }

        function showCreateSubTeamDialog(parentTeam) {
            teamDialogMode.value = "sub";
            teamForm.name = "";
            teamForm.description = "";
            teamForm.parentId = parentTeam.id;
            teamDialogVisible.value = true;
        }

        async function handleCreateTeam() {
            if (!teamForm.name) {
                ElMessage.warning("请输入团队名称");
                return;
            }
            try {
                if (teamDialogMode.value === "create") {
                    await API.createTeam(teamForm.name, teamForm.description);
                    ElMessage.success("团队创建成功");
                } else {
                    await API.createSubTeam(teamForm.parentId, teamForm.name, teamForm.description);
                    ElMessage.success("子团队创建成功");
                }
                teamDialogVisible.value = false;
                loadTeamTree();
            } catch (err) {
                ElMessage.error("创建失败：" + err.message);
            }
        }

        async function handleDeleteTeam(team) {
            try {
                await ElMessageBox.confirm(
                    `确认删除团队「${team.name}」？子团队和成员关联将一并删除。`,
                    "确认删除",
                    { type: "warning" }
                );
                await API.deleteTeam(team.id);
                ElMessage.success("团队已删除");
                loadTeamTree();
            } catch (err) {
                if (err !== "cancel" && err.message) ElMessage.error("删除失败：" + err.message);
            }
        }

        async function showTeamMembers(team) {
            currentTeam.value = team;
            teamMembersDialogVisible.value = true;
            teamMembersLoading.value = true;
            teamMembersList.value = [];
            try {
                teamMembersList.value = await API.getTeamMembers(team.id);
            } catch (err) {
                ElMessage.error("加载成员失败：" + err.message);
            } finally {
                teamMembersLoading.value = false;
            }
        }

        function showAddTeamMemberDialog() {
            addTeamMemberForm.memberId = "";
            addTeamMemberForm.role = "member";
            addTeamMemberDialogVisible.value = true;
        }

        async function handleAddTeamMember() {
            if (!addTeamMemberForm.memberId) {
                ElMessage.warning("请输入成员 ID");
                return;
            }
            try {
                await API.addTeamMember(currentTeam.value.id, addTeamMemberForm.memberId, addTeamMemberForm.role);
                ElMessage.success("成员已添加");
                addTeamMemberDialogVisible.value = false;
                teamMembersList.value = await API.getTeamMembers(currentTeam.value.id);
            } catch (err) {
                ElMessage.error("添加失败：" + err.message);
            }
        }

        async function handleRemoveTeamMember(memberId) {
            try {
                await ElMessageBox.confirm("确认移除该成员？", "确认", { type: "warning" });
                await API.removeTeamMember(currentTeam.value.id, memberId);
                ElMessage.success("成员已移除");
                teamMembersList.value = await API.getTeamMembers(currentTeam.value.id);
            } catch (err) {
                if (err !== "cancel" && err.message) ElMessage.error("移除失败：" + err.message);
            }
        }

        // 成员管理（系统级）
        async function loadMembers() {
            memberLoading.value = true;
            try {
                const [members, tags] = await Promise.all([API.listMembers(), API.listTags().catch(() => [])]);
                memberList.value = members;
                // 合并后端已有标签和默认建议
                const merged = [...new Set([...tags, ...["前端", "后端", "全栈", "测试", "运维", "DBA", "产品", "设计"]])];
                tagSuggestions.value = merged;
            } catch (err) {
                if (err.message.includes("403") || err.message.includes("无权")) {
                    ElMessage.warning("需要管理员权限");
                } else {
                    ElMessage.error("加载成员失败：" + err.message);
                }
                memberList.value = [];
            } finally {
                memberLoading.value = false;
            }
        }

        function showAddMemberDialog() {
            memberForm.memberId = "";
            memberForm.displayName = "";
            memberForm.role = "member";
            memberForm.tags = [];
            memberDialogVisible.value = true;
        }

        async function handleAddMember() {
            if (!memberForm.memberId) {
                ElMessage.warning("请输入成员 ID");
                return;
            }
            if (!memberForm.tags || memberForm.tags.length === 0) {
                ElMessage.warning("请至少选择一个成员标签");
                return;
            }
            try {
                await API.addMember(memberForm.memberId, memberForm.displayName, memberForm.role, memberForm.tags);
                ElMessage.success("成员已添加");
                memberDialogVisible.value = false;
                loadMembers();
            } catch (err) {
                ElMessage.error("添加失败：" + err.message);
            }
        }

        async function handleDeleteMember(memberId) {
            try {
                await ElMessageBox.confirm(`确认删除成员「${memberId}」？`, "确认删除", { type: "warning" });
                await API.deleteMember(memberId);
                ElMessage.success("成员已删除");
                loadMembers();
            } catch (err) {
                if (err !== "cancel" && err.message) ElMessage.error("删除失败：" + err.message);
            }
        }

        async function handleToggleMemberRole(member) {
            const newRole = member.role === "admin" ? "member" : "admin";
            try {
                await API.updateMember(member.member_id, { role: newRole });
                ElMessage.success(`角色已切换为 ${newRole}`);
                loadMembers();
            } catch (err) {
                ElMessage.error("修改失败：" + err.message);
            }
        }

        function showEditTagsDialog(member) {
            editTagsForm.memberId = member.member_id;
            editTagsForm.tags = [...(member.tags || [])];
            editTagsDialogVisible.value = true;
        }

        async function handleSaveTags() {
            try {
                await API.updateMember(editTagsForm.memberId, { tags: editTagsForm.tags });
                ElMessage.success("标签已更新");
                editTagsDialogVisible.value = false;
                loadMembers();
            } catch (err) {
                ElMessage.error("更新失败：" + err.message);
            }
        }

        // ---------------- 生命周期 ----------------
        onMounted(async () => {
            // 从 URL hash 恢复菜单状态
            const hash = window.location.hash.replace("#", "");
            const validMenus = ["my", "shared", "share-mgmt", "graph", "acl", "dashboard", "comm", "team", "members"];
            if (hash && validMenus.includes(hash)) {
                activeMenu.value = hash;
            }

            // 浏览器前进后退 → 恢复菜单
            window.addEventListener("popstate", (e) => {
                const menu = e.state?.menu;
                if (menu) {
                    activeMenu.value = menu;
                    handleMenuSelect(menu);
                }
            });

            if (await checkLogin()) {
                onMountedInit();
                // 如果 URL hash 指向非默认菜单，加载对应数据
                if (activeMenu.value !== "my") {
                    handleMenuSelect(activeMenu.value);
                }
            }
        });

        // 组件卸载时清理心跳定时器
        onUnmounted(() => {
            stopCommHeartbeat();
        });

        return {
            loggedIn, currentMember, currentRole, loginLoading, loginForm,
            showIssueDialog, issueLoading, issueForm,
            activeMenu, handleMenuSelect,
            myAssets, myLoading, myFilter, myPage, loadMyAssets,
            sharedAssets, sharedLoading, sharedFilter, sharedPage, loadSharedAssets,
            shareMgmtAssets, shareMgmtLoading, shareMgmtFilter, shareMgmtSelection,
            handleShareMgmtSelectionChange, showBatchScopeDialog, handleBatchUpdateScope,
            quickChangeScope, batchScopeDialogVisible, batchScopeNewValue, batchScopeUpdating,
            detailDialogVisible, detailData, detailLoading, showDetail,
            scopeDialogVisible, scopeDialogData, scopeDialogNewValue, scopeUpdating,
            showScopeDialog, handleUpdateScope,
            graphRootId, graphDepth, graphData, graphLoading, loadGraph, loadGraphRoot,
            linkDialogVisible, linkForm, linkLoading, showAddLinkDialog, handleAddLink,
            currentLinksAsset, linksDialogVisible, linksData, linksLoading, showLinksDialog, handleDeleteLink,
            aclAssets, aclLoading, loadAclAssets, aclDialogVisible, aclData, aclLoading2,
            showAclDialog, currentAclAsset, aclFormDialogVisible, aclForm, aclFormLoading,
            showAddAclDialog, handleAddAcl, handleDeleteAcl,
            memberStats, loadMemberStats,
            // 影子通信
            commTab, peersList, peersLoading, commHeartbeatRunning, onlinePeersCount,
            loadPeers, handleCommTabChange,
            convList, convLoading, convFilter, loadConversations, showPeerConversations,
            askForm, askLoading, showAskFromPeer, clearAskForm, handleAskPeer,
            chatMessages, chatBoxRef, loadChatHistory, chatMessageText, formatChatTime, peerOnline,
            shadowList, shadowLoading, shadowFilter, loadShadowLog, handleReconcile,
            threadDialogVisible, threadData, threadLoading, showThreadDialog,
            eventTypeTag, statusTagType,
            handleLogin, logout, handleIssueKey,
            // 团队管理
            teamTree, teamLoading, teamDialogVisible, teamForm, teamDialogMode,
            loadTeamTree, showCreateTeamDialog, showCreateSubTeamDialog,
            handleCreateTeam, handleDeleteTeam,
            currentTeam, teamMembersDialogVisible, teamMembersList, teamMembersLoading,
            showTeamMembers, showAddTeamMemberDialog, handleAddTeamMember, handleRemoveTeamMember,
            addTeamMemberDialogVisible, addTeamMemberForm,
            // 成员管理
            memberList, memberLoading, memberDialogVisible, memberForm,
            tagSuggestions, editTagsDialogVisible, editTagsForm,
            showAddMemberDialog, handleAddMember, handleDeleteMember,
            handleToggleMemberRole, showEditTagsDialog, handleSaveTags,
            scopeLabel: (s) => Utils.scopeLabel(s),
            typeTagType: (t) => Utils.typeTagType(t),
            linkTypeLabel: (t) => Utils.linkTypeLabel(t),
            formatDate: (dt) => Utils.formatDate(dt),
            Refresh,
        };
    },
});

for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
    app.component(name, comp);
}
app.use(ElementPlus);
// 挂载根实例到 window，供自动化测试调用组件方法（prod build 下 __vue_app__._instance 为 null）
window.__teamharness_vm = app.mount("#app");
