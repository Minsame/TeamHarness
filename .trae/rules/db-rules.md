# db-rules.md

> 本文件存放 数据库操作（SQLAlchemy / ORM / schema）相关的工程踩坑规则
> 来源：多项目经验提炼，跨项目复用。
> 追溯链格式：`[来源类型: 新提炼|导入 | 来源: ...]`

## 数据库

### SQLAlchemy 隐性陷阱
- **ORM `default=` 是 Python 端默认值**，仅 ORM insert 时触发；原始 SQL `text()` INSERT 不触发，须用 `server_default` 或显式包含 NOT NULL 列值。SQLite 下 `BigInteger, primary_key=True, autoincrement=True` 不自增，须用 `BigInteger().with_variant(Integer, "sqlite")`。
- **schema 变更须同步测试断言**：唯一约束改名/加字段后，`test_schema.py` 中的约束名断言须同步更新，否则跨域 schema 测试断裂。
`[来源类型: 新提炼 | 来源: Agent 2 / SubTask 2.11 + Agent 5 / SubTask 5.8 / 第一波+第二波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`


### 测试 DB 后端必须覆盖生产 DB 驱动与特性
测试用 SQLite 内存库绕过生产 DB 驱动（如 psycopg / asyncpg）和特性（pgvector / server_default / JSONB），导致依赖声明缺失或 PG 特有 SQL 在生产才暴露。单元测试用 SQLite 作快速反馈合理，但合并前必须有 PG 后端测试覆盖驱动加载、扩展、PG 特有语法。
- **判定规则**：CI 测试矩阵至少包含 SQLite（快速反馈）+ 生产 DB（PG/MySQL）。依赖声明变更时验证 `pip install -e .` 或 `pip install --dry-run` 成功（非仅 `-r requirements.txt`）。schema 变更须在 PG 后端验证 `server_default` / 扩展（pgvector） / 索引类型。生产默认启用的依赖不可声明为 optional（`pip install -e .` 默认不装）
- **反例**：pyproject.toml 缺 `psycopg` 声明，但 docker-compose 用 `postgresql+psycopg://` → `pip install -e .` 装不上 psycopg，生产运行时 `ModuleNotFoundError`；所有测试用 SQLite 内存库不加载 psycopg，永远不触发。延伸：pgvector 声明为 `[project.optional-dependencies].pgvector`，但 docker-compose 默认 `TEAMHARNESS_VECTOR_STORE: pgvector` → 生产默认需要的依赖被声明为可选
- **正例**：CI 用 testcontainers 起真实 PG 容器跑集成测试，验证 psycopg 驱动加载 + pgvector 扩展可用 + `server_default` 生效；pyproject.toml 为依赖单一来源，生产默认启用的依赖放入 `[project].dependencies` 而非 optional
- 适用范围：所有"SQLite 测试 + PG 生产"架构，以及依赖声明与测试 DB 解耦的场景
`[来源类型: 新提炼 | 来源: 依赖分析 / pyproject.toml + binding/tests/conftest.py / 2026-08-10]`
