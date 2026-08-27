"""跨项目再升维模块。

对应 resource-harness 规则的「跨项目再升维」环节（跨项目升维与连锁更新.md）。

核心职责：
- 扫描各项目归档经验，找可被同一抽象模式解释的 2+ 个经验
- 将抽象模式升维为全局规则（写入 global_rules_dir）
- 在图谱中登记升维关系（PROMOTED_FROM）和同源关系（SAME_SOURCE）

再升维与普通升维的关键区别：
- 普通升维：单条经验 → 更高层级规则
- 跨项目再升维：多条经验（可能不相似）→ 同一抽象模式 → 全局规则
  关键：再升维的对象不是"相似经验"，而是"可被同一抽象模式解释的经验"。
  不相似的也可能合并（底层共性），相似的也可能不能合并（表面相似但本质不同）。

再升维触发时机（跨项目升维与连锁更新.md）：
- 被动触发：新经验提炼时，检查是否已有其他项目经验可被同一模式解释（2+ 个经验）
- 定期触发：与热点规则评估同期（每 10 次会话）
- 手动触发：用户明确要求"整理各项目经验"

再升维流程（跨项目升维与连锁更新.md）：
1. 收集：扫描各项目记忆，找 2+ 个经验（不需要相似）
2. 找抽象模式：穿透表面差异，找底层共性规律
3. 升维：从具体经验抽象为通用模式
4. 回测：用升维后的模式逐一回测所有原始案例
5. 图谱登记：在 graph.md 中记录升维关系（升维自、同源）
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from server.distill_team.promotion.adapters.base import (
    CodingSoftwareAdapter,
    MemoryLayout,
)
from server.distill_team.promotion.archive import ArchiveManager
from server.distill_team.promotion.graph import GraphRegistry
from server.distill_team.promotion.models import (
    ArchiveEntry,
    GraphNode,
    GraphRelation,
    GraphRelationType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

# 共有关键词数量阈值：2+ 个经验共有 >= 此值的关键词 → 认为可被同一模式解释
COMMON_KEYWORD_THRESHOLD = 2

# 关键词提取的最小长度（过滤短词噪声）
_KEYWORD_MIN_LENGTH = 2

# 停用词表（过滤无意义高频词，与 retest.py 保持一致）
_STOP_WORDS = frozenset({
    # 中文停用词
    "的", "了", "在", "是", "和", "与", "或", "及", "也", "都",
    "不", "要", "会", "能", "可", "以", "对", "为", "由", "从",
    "这", "那", "其", "之", "于", "等", "被", "把", "让", "使",
    # 英文停用词
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "and", "or", "not", "no", "for", "with", "in", "on", "at",
    "to", "of", "as", "by", "this", "that", "it", "from", "if",
    "then", "else", "when", "while", "do", "did", "has", "have",
})

# archive.md 字段行正则（与 archive.py 保持一致）
_FIELD_RE = re.compile(r"^-\s+(.+?)：(.*)$")


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class CrossProjectPattern:
    """跨项目抽象模式。

    对应跨项目升维与连锁更新.md 的"抽象模式"：从多个不相似但可被同一模式
    解释的经验中，穿透表面差异，找出的底层共性规律。
    """

    pattern_id: str  # 如 P001
    name: str  # 模式名称
    description: str  # 模式描述（底层共性规律）
    source_entries: list[str]  # 来源经验 ID 列表（如 ["E001", "E007"]）
    source_projects: list[str]  # 来源项目路径列表
    abstract_rule_content: str  # 升维后的通用规则内容


@dataclass
class CrossProjectResult:
    """跨项目再升维结果。"""

    triggered: bool  # 是否触发了再升维
    patterns: list[CrossProjectPattern]  # 发现的模式
    new_global_rules: list[str]  # 新升维的全局规则 ID 列表
    graph_relations_added: list[GraphRelation]  # 新增的图谱关系


# ---------------------------------------------------------------------------
# 关键词提取（内部辅助）
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """简单分词：提取中英文词组。

    中文按连续字符序列切分，英文按标识符切分。
    简化实现，不依赖外部分词库。
    """
    return re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z_][a-zA-Z0-9_]*", text)


def _extract_keywords(text: str) -> set[str]:
    """提取文本关键词（去除停用词和短词）。

    返回关键词集合（小写）。
    """
    keywords: set[str] = set()
    for token in _tokenize(text):
        token_lower = token.lower()
        if token_lower in _STOP_WORDS:
            continue
        if len(token_lower) < _KEYWORD_MIN_LENGTH:
            continue
        keywords.add(token_lower)
    return keywords


def _entry_text(entry: ArchiveEntry) -> str:
    """经验的文本（标题 + 原始案例），用于关键词提取。"""
    return f"{entry.title}\n{entry.original_case}"


# ---------------------------------------------------------------------------
# 抽象模式识别
# ---------------------------------------------------------------------------


def _generate_pattern_id(entry_ids: list[str]) -> str:
    """根据经验 ID 列表生成稳定的模式 ID（P001 格式）。

    使用 MD5 哈希保证同一组经验生成相同 ID（跨进程稳定）。
    """
    id_str = ",".join(sorted(entry_ids))
    hash_val = hashlib.md5(id_str.encode("utf-8")).hexdigest()
    num = int(hash_val[:6], 16) % 1000 + 1
    return f"P{num:03d}"


def find_abstract_pattern(
    entries: list[ArchiveEntry],
) -> CrossProjectPattern | None:
    """从多个经验中识别抽象模式。

    简化实现：
    1. 提取每个经验的关键词（去除停用词）
    2. 找多个经验共有的关键词集合（交集）
    3. 若共有关键词 >= COMMON_KEYWORD_THRESHOLD → 认为可被同一模式解释
    4. 生成模式描述：基于共有关键词组合

    关键区别：再升维的对象不是"相似经验"，而是"可被同一抽象模式解释的经验"。
    不相似的也可能合并（底层共性），相似的也可能不能合并（表面相似但本质不同）。

    Args:
        entries: 待识别的经验列表（需 >= 2 个）

    Returns:
        CrossProjectPattern 或 None（无法识别模式时）
    """
    if len(entries) < 2:
        return None

    # 提取每个经验的关键词
    keywords_list = [_extract_keywords(_entry_text(e)) for e in entries]

    # 找共有关键词（所有经验的交集）
    common_keywords = keywords_list[0]
    for kw_set in keywords_list[1:]:
        common_keywords = common_keywords & kw_set

    # 共有关键词不足 → 无法识别模式
    if len(common_keywords) < COMMON_KEYWORD_THRESHOLD:
        return None

    # 生成模式 ID（基于经验 ID 哈希，保证同一组经验生成相同 ID）
    entry_ids = sorted(e.entry_id for e in entries)
    pattern_id = _generate_pattern_id(entry_ids)

    # 生成模式名称：基于共有关键词
    sorted_keywords = sorted(common_keywords)
    name = "、".join(sorted_keywords[:3])  # 取前 3 个关键词作为名称
    if len(sorted_keywords) > 3:
        name += " 等"

    # 生成模式描述
    description = (
        f"多个项目经验共有的底层共性：涉及 {name}。"
        f"来源经验：{', '.join(entry_ids)}。"
        f"共有关键词：{', '.join(sorted_keywords)}。"
    )

    # 生成升维后的通用规则内容
    abstract_rule_content = _build_abstract_rule_content(entries, sorted_keywords)

    return CrossProjectPattern(
        pattern_id=pattern_id,
        name=name,
        description=description,
        source_entries=list(entry_ids),
        source_projects=[],  # 由调用方填充
        abstract_rule_content=abstract_rule_content,
    )


def _build_abstract_rule_content(
    entries: list[ArchiveEntry], common_keywords: list[str]
) -> str:
    """根据来源经验和共有关键词构建升维后的通用规则内容。

    格式：
    ## 跨项目通用规则

    **底层共性规律**：...

    **适用场景**：
    - {关键词1}
    - {关键词2}

    **来源经验**：
    - {E001}：{标题1}
    - {E007}：{标题2}
    """
    keyword_lines = "\n".join(f"- {kw}" for kw in common_keywords)
    source_lines = "\n".join(f"- {e.entry_id}：{e.title}" for e in entries)
    return (
        f"## 跨项目通用规则\n\n"
        f"**底层共性规律**："
        f"多个项目经验可被同一抽象模式解释，"
        f"共有关键词：{', '.join(common_keywords)}。\n\n"
        f"**适用场景**：\n{keyword_lines}\n\n"
        f"**来源经验**：\n{source_lines}\n"
    )


# ---------------------------------------------------------------------------
# 项目记忆扫描
# ---------------------------------------------------------------------------


def scan_project_memories(
    cross_project_root: Path,
) -> dict[str, list[ArchiveEntry]]:
    """扫描各项目记忆，返回 {项目路径: [经验列表]} 映射。

    简化处理：
    1. 遍历 cross_project_root 下的所有子目录
    2. 每个子目录视为一个项目
    3. 在子目录下查找 archive.md 或 *.md 文件
    4. 用 archive.md 格式解析（若有 archive.md）或简单解析其他 .md 文件

    不同项目的记忆结构可能不同，简化处理：
    - archive.md：按归档区格式解析（### E001：标题 + 字段行）
    - 其他 .md 文件：简单解析，每个 ### 标题视为一条经验
    """
    result: dict[str, list[ArchiveEntry]] = {}

    if not cross_project_root.exists():
        return result

    # 遍历子目录
    for project_dir in cross_project_root.iterdir():
        if not project_dir.is_dir():
            continue

        project_path = str(project_dir)
        entries: list[ArchiveEntry] = []

        # 优先查找 archive.md（按归档区格式解析）
        archive_md = project_dir / "archive.md"
        if archive_md.is_file():
            try:
                text = archive_md.read_text(encoding="utf-8")
                entries.extend(_parse_archive_text(text))
            except (OSError, UnicodeDecodeError) as e:
                logger.warning("解析 archive.md 失败: %s: %s", archive_md, e)

        # 若 archive.md 不存在或无经验条目，扫描其他 .md 文件
        if not entries:
            for md_file in sorted(project_dir.rglob("*.md")):
                if md_file.name == "archive.md":
                    continue
                try:
                    file_entries = _simple_parse_md(md_file)
                    entries.extend(file_entries)
                except (OSError, UnicodeDecodeError) as e:
                    logger.warning("解析 .md 文件失败: %s: %s", md_file, e)

        if entries:
            result[project_path] = entries

    return result


def _parse_archive_text(text: str) -> list[ArchiveEntry]:
    """解析 archive.md 格式的文本，返回经验条目列表。

    复用 archive.md 的格式规范（与 archive.py 的解析逻辑一致）：
        ### E001：标题
        - 升维至：R005
        - 升维时间：2026-08-11
        - 原始错误案例：...
        - 升维策略：...
        - 来源会话：...
    """
    entries: list[ArchiveEntry] = []

    # 按 ### E001： 切分
    parts = re.split(r"^###\s+(E\d+)：", text, flags=re.MULTILINE)
    # parts = [pre_text, entry_id_1, body_1, entry_id_2, body_2, ...]

    i = 1
    while i < len(parts):
        entry_id = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        entry = _parse_entry_body(entry_id, body)
        if entry:
            entries.append(entry)
        i += 2

    return entries


def _parse_entry_body(entry_id: str, body: str) -> ArchiveEntry | None:
    """解析单条经验条目。

    body 是 ### E001： 之后的文本，首行为标题，后续为字段行。
    原始错误案例字段可能跨多行（直到下一个 - 字段行）。
    """
    lines = body.split("\n")
    if not lines:
        return None

    title = lines[0].strip()

    fields: dict[str, str] = {}
    original_case_lines: list[str] = []
    in_original_case = False

    for line in lines[1:]:
        if in_original_case:
            # 原始案例多行收集：遇到下一个 - 字段行则结束
            if line.startswith("- "):
                in_original_case = False
                m = _FIELD_RE.match(line)
                if m:
                    fields[m.group(1)] = m.group(2).strip()
            else:
                original_case_lines.append(line)
            continue

        m = _FIELD_RE.match(line)
        if m:
            key = m.group(1)
            val = m.group(2).strip()
            if key == "原始错误案例":
                in_original_case = True
                original_case_lines.append(val)
            else:
                fields[key] = val

    return ArchiveEntry(
        entry_id=entry_id,
        title=title,
        promoted_to=fields.get("升维至", ""),
        promoted_at=_parse_date(fields.get("升维时间", "")),
        original_case="\n".join(original_case_lines).strip(),
        promotion_strategy=fields.get("升维策略", ""),
        source_session=fields.get("来源会话", ""),
    )


def _simple_parse_md(md_file: Path) -> list[ArchiveEntry]:
    """简单解析 .md 文件为 ArchiveEntry 列表。

    简化处理：每个 ### 标题视为一条经验，标题后的内容为原始案例。
    若无 ### 标题，整个文件视为一条经验。
    """
    text = md_file.read_text(encoding="utf-8")
    entries: list[ArchiveEntry] = []

    # 按 ### 标题切分
    parts = re.split(r"^###\s+(.+)$", text, flags=re.MULTILINE)
    # parts = [pre_text, title_1, body_1, title_2, body_2, ...]

    if len(parts) <= 1:
        # 无 ### 标题，整个文件视为一条经验
        if text.strip():
            entries.append(
                ArchiveEntry(
                    entry_id=f"{md_file.stem.upper()[:8]}001",
                    title=md_file.stem,
                    promoted_to="",
                    promoted_at=datetime.min,
                    original_case=text.strip(),
                    promotion_strategy="",
                    source_session="",
                )
            )
        return entries

    i = 1
    entry_num = 0
    while i < len(parts):
        title = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        entry_num += 1
        entry_id = f"{md_file.stem.upper()[:8]}{entry_num:03d}"
        entries.append(
            ArchiveEntry(
                entry_id=entry_id,
                title=title,
                promoted_to="",
                promoted_at=datetime.min,
                original_case=body,
                promotion_strategy="",
                source_session="",
            )
        )
        i += 2

    return entries


def _parse_date(s: str) -> datetime:
    """解析日期字符串，失败返回 datetime.min。"""
    s = s.strip()
    if not s:
        return datetime.min
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.min


# ---------------------------------------------------------------------------
# 跨项目再升维管理器
# ---------------------------------------------------------------------------


class CrossProjectPromoter:
    """跨项目再升维管理器。

    对应 resource-harness 规则的"跨项目再升维"环节。

    用法：
        promoter = CrossProjectPromoter(adapter, graph, archive)
        # 被动触发
        result = promoter.check_and_promote(layout)
        # 手动触发
        patterns = promoter.scan_cross_project_patterns(layout)
        for p in patterns:
            promoter.promote_pattern(p, layout)
    """

    def __init__(
        self,
        adapter: CodingSoftwareAdapter,
        graph: GraphRegistry,
        archive: ArchiveManager,
    ) -> None:
        self._adapter = adapter
        self._graph = graph
        self._archive = archive

    def scan_cross_project_patterns(
        self, layout: MemoryLayout
    ) -> list[CrossProjectPattern]:
        """扫描各项目记忆，找可再升维的模式。

        1. 遍历 layout.cross_project_root 下的各项目目录
        2. 收集各项目的归档经验
        3. 找 2+ 个可被同一模式解释的经验
        4. 返回模式列表
        """
        # 1. 扫描各项目记忆
        project_entries = scan_project_memories(layout.cross_project_root)

        if not project_entries:
            return []

        # 2. 收集所有经验，记录来源项目
        all_entries: list[tuple[ArchiveEntry, str]] = []
        for project_path, entries in project_entries.items():
            for entry in entries:
                all_entries.append((entry, project_path))

        if len(all_entries) < 2:
            return []

        # 3. 贪心聚类：找可被同一模式解释的经验组
        # 以每个未使用的经验为种子，贪婪地加入可合并的其他经验
        patterns: list[CrossProjectPattern] = []
        used_indices: set[int] = set()

        for i, (entry_i, project_i) in enumerate(all_entries):
            if i in used_indices:
                continue

            # 以 entry_i 为种子，找其他可合并的经验
            group_entries: list[ArchiveEntry] = [entry_i]
            group_projects: list[str] = [project_i]
            group_indices: list[int] = [i]

            for j in range(i + 1, len(all_entries)):
                if j in used_indices:
                    continue
                entry_j, project_j = all_entries[j]
                # 尝试将 entry_j 加入当前组：若加入后仍可识别抽象模式则接受
                trial_entries = group_entries + [entry_j]
                trial_pattern = find_abstract_pattern(trial_entries)
                if trial_pattern is not None:
                    group_entries.append(entry_j)
                    group_projects.append(project_j)
                    group_indices.append(j)

            # 若组内有 2+ 个经验 → 生成模式
            if len(group_entries) >= 2:
                pattern = find_abstract_pattern(group_entries)
                if pattern is not None:
                    # 填充来源项目（find_abstract_pattern 不感知项目路径）
                    pattern.source_projects = list(group_projects)
                    patterns.append(pattern)
                    for idx in group_indices:
                        used_indices.add(idx)

        return patterns

    def promote_pattern(
        self, pattern: CrossProjectPattern, layout: MemoryLayout
    ) -> CrossProjectResult:
        """将模式升维为全局规则。

        1. 写入全局规则文件（layout.global_rules_dir）
        2. 在图谱中注册新规则节点
        3. 添加 PROMOTED_FROM 关系（新规则 → 各来源经验）
        4. 添加 SAME_SOURCE 关系（各来源经验之间）
        """
        # 1. 生成新规则 ID（R 开头，基于图谱已有节点递增）
        rule_id = self._next_rule_id()

        # 2. 写入全局规则文件
        rule_path = self._adapter.write_rule(
            rules_dir=layout.global_rules_dir,
            rule_id=rule_id,
            title=pattern.name,
            content=pattern.abstract_rule_content,
            frontmatter=None,
        )

        # 3. 注册图谱节点
        self._graph.register_node(
            GraphNode(
                node_id=rule_id,
                node_type="rule",
                name=pattern.name,
                location=str(rule_path),
                category="跨项目通用规则",
                status="active",
            )
        )

        relations_added: list[GraphRelation] = []

        # 4. 添加 PROMOTED_FROM 关系（新规则 → 各来源经验）
        for entry_id in pattern.source_entries:
            relation = GraphRelation(
                source_id=rule_id,
                target_id=entry_id,
                relation_type=GraphRelationType.PROMOTED_FROM,
                note=f"跨项目再升维：模式 {pattern.pattern_id}",
            )
            self._graph.add_relation(relation)
            relations_added.append(relation)

        # 5. 添加 SAME_SOURCE 关系（各来源经验之间，两两建立）
        for i, entry_i in enumerate(pattern.source_entries):
            for entry_j in pattern.source_entries[i + 1:]:
                relation = GraphRelation(
                    source_id=entry_i,
                    target_id=entry_j,
                    relation_type=GraphRelationType.SAME_SOURCE,
                    note=f"同源：模式 {pattern.pattern_id}",
                )
                self._graph.add_relation(relation)
                relations_added.append(relation)

        return CrossProjectResult(
            triggered=True,
            patterns=[pattern],
            new_global_rules=[rule_id],
            graph_relations_added=relations_added,
        )

    def check_and_promote(
        self, layout: MemoryLayout
    ) -> CrossProjectResult:
        """被动触发：检查并执行跨项目再升维。

        scan_cross_project_patterns → 找到模式 → promote_pattern
        """
        patterns = self.scan_cross_project_patterns(layout)

        if not patterns:
            return CrossProjectResult(
                triggered=False,
                patterns=[],
                new_global_rules=[],
                graph_relations_added=[],
            )

        # 对每个模式执行升维
        all_relations: list[GraphRelation] = []
        all_new_rules: list[str] = []
        for pattern in patterns:
            result = self.promote_pattern(pattern, layout)
            all_relations.extend(result.graph_relations_added)
            all_new_rules.extend(result.new_global_rules)

        return CrossProjectResult(
            triggered=True,
            patterns=patterns,
            new_global_rules=all_new_rules,
            graph_relations_added=all_relations,
        )

    def _next_rule_id(self) -> str:
        """生成下一个规则 ID（R001 格式，基于图谱已有节点递增）。

        扫描图谱中所有 R 开头的节点 ID，取最大值 +1。
        """
        nodes = self._graph.list_nodes()
        max_num = 0
        for node in nodes:
            m = re.match(r"^R(\d+)$", node.node_id)
            if m:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num
        return f"R{max_num + 1:03d}"


__all__ = [
    "CrossProjectPattern",
    "CrossProjectPromoter",
    "CrossProjectResult",
    "find_abstract_pattern",
    "scan_project_memories",
]
