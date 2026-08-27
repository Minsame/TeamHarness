// 共享工具函数 — 供各页面使用

window.TeamHarnessUtils = {
    scopeLabels: {
        private: "私有",
        team: "团队",
        restricted: "受限",
        public: "公开",
    },

    scopeLabel(scope) {
        return this.scopeLabels[scope] || scope;
    },

    typeTagTypes: {
        rule: "primary",
        memory: "success",
        skill: "warning",
        tool: "danger",
        prompt: "info",
    },

    typeTagType(type) {
        return this.typeTagTypes[type] || "";
    },

    linkTypeLabels: {
        derived_from: "派生自",
        supersedes: "取代",
        related_to: "关联",
        module_parent: "模块父级",
        triggers: "触发",
    },

    linkTypeLabel(t) {
        return this.linkTypeLabels[t] || t;
    },

    formatDate(dt) {
        if (!dt) return "—";
        try {
            const d = new Date(dt);
            return d.toLocaleString("zh-CN", { hour12: false });
        } catch (_) {
            return dt;
        }
    },
};
