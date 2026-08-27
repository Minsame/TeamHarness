-- TeamHarness 测试数据（前端演示用）
-- 插入不同 owner / scope / type 的资产，验证前端展示与过滤

INSERT INTO asset_index (id, type, owner, scope, content_hash, version, tags, related_to, git_path, git_commit, module_path, category, status, schema_version, created_at, updated_at, indexed_at, content_snapshot) VALUES
-- alice 的资产
('asset-alice-001', 'rule', 'alice', 'private', 'hash-001', '1.0.0', '["coding","style"]', '[]', 'rules/alice/coding-style.md', 'abc1234', 'modules/backend', 'backend/coding', 'active', 1, NOW(), NOW(), NOW(), '编码风格规则：函数长度不超过 50 行，命名使用 snake_case，禁止全局变量。'),
('asset-alice-002', 'memory', 'alice', 'team', 'hash-002', '1.0.0', '["postgres","sql"]', '[]', 'memory/alice/pg-tips.md', 'abc1234', 'modules/backend/db', 'backend/sql', 'active', 1, NOW(), NOW(), NOW(), 'PostgreSQL 性能优化：索引选择性 = 基数 / 总行数，选择性 > 0.3 适合建索引。EXPLAIN ANALYZE 查看实际执行计划。'),
('asset-alice-003', 'skill', 'alice', 'team', 'hash-003', '1.0.0', '["debugging","tracing"]', '[]', 'skills/alice/debug-trace.md', 'abc1234', 'modules/backend', 'backend/debug', 'active', 1, NOW(), NOW(), NOW(), '调试技能：分布式追踪三步法 1) 复现问题 2) 定位边界 3) 二分排除。使用 strace/ltrace/gdb 组合。'),
('asset-alice-004', 'prompt', 'alice', 'private', 'hash-004', '1.0.0', '["review","pr"]', '[]', 'prompts/alice/pr-review.md', 'abc1234', 'modules/governance', 'governance/review', 'active', 1, NOW(), NOW(), NOW(), 'PR Review 提示词模板：检查 1) 代码风格 2) 安全漏洞 3) 性能问题 4) 测试覆盖。输出格式：{severity, category, suggestion}'),
('asset-alice-005', 'rule', 'alice', 'public', 'hash-005', '1.0.0', '["api","rest"]', '[]', 'rules/alice/api-design.md', 'abc1234', 'modules/backend/api', 'backend/api', 'active', 1, NOW(), NOW(), NOW(), 'API 设计规则：RESTful 命名用复数名词，版本放 URL 前缀，分页用 limit/offset，错误用统一 JSON 结构。'),

-- bob 的资产
('asset-bob-001', 'rule', 'bob', 'team', 'hash-006', '1.0.0', '["frontend","react"]', '[]', 'rules/bob/react-patterns.md', 'def5678', 'modules/frontend', 'frontend/react', 'active', 1, NOW(), NOW(), NOW(), 'React 模式规则：组件拆分单一职责，状态提升到最近公共父，副作用用 useEffect 清理。禁用 any 类型。'),
('asset-bob-002', 'memory', 'bob', 'private', 'hash-007', '1.0.0', '["css","layout"]', '[]', 'memory/bob/css-tricks.md', 'def5678', 'modules/frontend', 'frontend/css', 'active', 1, NOW(), NOW(), NOW(), 'CSS 布局记忆：flex 用于一维，grid 用于二维。居中三行代码：display:flex; justify-content:center; align-items:center。'),
('asset-bob-003', 'tool', 'bob', 'team', 'hash-008', '1.0.0', '["build","vite"]', '[]', 'tools/bob/vite-config.md', 'def5678', 'modules/frontend/build', 'frontend/build', 'active', 1, NOW(), NOW(), NOW(), 'Vite 构建工具配置：依赖预构建 optimizeDeps，代码分割 manualChunks，CDN 外部化 external。'),
('asset-bob-004', 'skill', 'bob', 'public', 'hash-009', '1.0.0', '["testing","unit"]', '[]', 'skills/bob/unit-test.md', 'def5678', 'modules/frontend', 'frontend/test', 'active', 1, NOW(), NOW(), NOW(), '单元测试技能：AAA 模式（Arrange-Act-Assert），每个用例只测一个行为，mock 外部依赖，覆盖率 > 80%。'),

-- charlie 的资产
('asset-charlie-001', 'rule', 'charlie', 'team', 'hash-010', '1.0.0', '["devops","docker"]', '[]', 'rules/charlie/docker.md', 'ghi9012', 'modules/deploy', 'deploy/docker', 'active', 1, NOW(), NOW(), NOW(), 'Docker 规则：多阶段构建减小镜像，.dockerignore 排除 node_modules，非 root 用户运行，HEALTHCHECK 必加。'),
('asset-charlie-002', 'memory', 'charlie', 'restricted', 'hash-011', '1.0.0', '["security","auth"]', '[]', 'memory/charlie/security.md', 'ghi9012', 'modules/governance', 'governance/security', 'active', 1, NOW(), NOW(), NOW(), '安全记忆：JWT 不要存 localStorage（XSS），用 httpOnly cookie。密码用 bcrypt cost=12。CSRF 用 SameSite=Lax。'),
('asset-charlie-003', 'tool', 'charlie', 'team', 'hash-012', '1.0.0', '["ci","github"]', '[]', 'tools/charlie/ci-pipeline.md', 'ghi9012', 'modules/deploy', 'deploy/ci', 'active', 1, NOW(), NOW(), NOW(), 'CI 流水线工具：GitHub Actions 矩阵测试，缓存依赖加速，并行 jobs，失败 fast-fail。'),
('asset-charlie-004', 'prompt', 'charlie', 'private', 'hash-013', '1.0.0', '["doc","readme"]', '[]', 'prompts/charlie/readme.md', 'ghi9012', 'modules/docs', 'docs/readme', 'active', 1, NOW(), NOW(), NOW(), 'README 生成提示词：项目简介 + 快速开始 + 安装步骤 + 使用示例 + 配置说明 + 贡献指南。'),

-- 一些已废弃的资产
('asset-alice-006', 'rule', 'alice', 'private', 'hash-014', '0.9.0', '[]', '[]', 'rules/alice/old-style.md', 'old1234', 'modules/backend', 'backend/coding', 'superseded', 1, NOW() - INTERVAL '30 days', NOW() - INTERVAL '30 days', NOW() - INTERVAL '30 days', '旧版编码规则（已废弃）');

SELECT '插入完成，总数：' || COUNT(*) FROM asset_index;
