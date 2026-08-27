# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec：将 TeamHarness 打包为单二进制（All-in-One 模式，SubTask 3.1）
#
# 用法：
#   pip install pyinstaller
#   pyinstaller deploy/all_in_one.spec --noconfirm --clean
#
# 产物：
#   dist/teamharness-all-in-one    （Linux/macOS 单可执行文件）
#   dist/teamharness-all-in-one.exe（Windows）
#
# 设计要点：
# 1. 内嵌 SQLite + sqlite-vec + libgit2 三件套，5 人团队下载即可启动
# 2. SQLite 是 Python stdlib，无需额外打包；sqlite-vec / pygit2 需带 native 扩展
# 3. FastAPI / uvicorn / pyyaml 等纯 Python 包按 --hidden-import 兜底
# 4. 数据目录运行时由 TEAMHARNESS_DATA_DIR 或 ~/.teamharness/data 决定

block_cipher = None

a = Analysis(
    ['deploy/all_in_one_entry.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        # 数据文件：无（runtime 生成）
    ],
    hiddenimports=[
        # FastAPI / uvicorn
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # sqlite-vec native 扩展
        'sqlite_vec',
        # pygit2 + libgit2 绑定
        'pygit2',
        # 项目内模块（按 Agent 完成情况陆续加入）
        'server.app',
        'server.deploy',
        'server.deploy.all_in_one',
        'server.deploy.config',
        'server.deploy.api_versioning',
        'server.deploy.schema_version',
        'server.deploy.backup',
        'server.deploy.migrations',
        'server.common.models',
        'server.infra_git',
        'server.infra_git.git_provider',
        'server.infra_git.webhook',
        'server.infra_git.index_manager',
        'server.infra_git.trae_adapter',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除 All-in-One 不需要的依赖（PG/Qdrant 走 docker-compose 模式）
        'psycopg2',
        'pgvector',
        'qdrant_client',
        'alembic',
        'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='teamharness-all-in-one',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                 # 用 UPX 压缩，减小体积
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,             # 单二进制启动需要终端日志输出
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='deploy/teamharness.ico',  # 按需添加图标
)
