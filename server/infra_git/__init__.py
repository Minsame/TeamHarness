"""infra_git 领域包：仓库与 Git Provider 抽象层。

包含：
- GitProvider 抽象与 GitLab/Gitea/libgit2 三实现（git_provider.py）
- 分层仓库 INDEX.md 解析与防孤岛校验（index_manager.py）
- webhook 接收端点（webhook.py）
- Trae 深度适配：frontmatter 双区 + 会话路径探测（trae_adapter.py）
- categories.yaml 受控词汇表（categories.py）
- DREAMS.md 按月切分（dreams.py）
- restricted 读权限（restricted.py）
"""

from server.infra_git.git_provider import (
    GitLabProvider,
    GiteaProvider,
    GitProvider,
    Libgit2Provider,
    RepoSizeAlarm,
    create_git_provider,
)

__all__ = [
    "GitProvider",
    "GitLabProvider",
    "GiteaProvider",
    "Libgit2Provider",
    "RepoSizeAlarm",
    "create_git_provider",
]
