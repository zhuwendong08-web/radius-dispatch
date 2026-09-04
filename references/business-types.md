# 业务类型配置模板

`scripts/radius_dispatch.py` 顶部的 `BUSINESS_TYPES` 字典，就是「业务类型参数化」的落点。
每种业务类型 = 一套「约束模板 + POI 类目映射 + 打分权重」。

## 配置结构

```python
"<type-key>": {
    "label": "中文名",
    "summary": "一句话场景说明，会原样写进 report.md",
    "osm_tags": {"<osm-key>": ["<value>", ...]},   # 标签精确匹配（拼成正则）
    "name_keywords": ["关键词", ...],               # 名称关键词兜底
    "weights": {"transit": 0.5, "size": 0.3, "capacity": 0.2},  # 三项权重，建议和=1
    "defaults": {
        "max_transit_distance": 1000,  # 交通硬约束（米）
        "target_capacity": 250,         # 目标容量（人）；0 = 不启用容量分
        "full_score_area": 5000,        # 面积满分线（m²）
    },
},
```

## 三个内置类型的设计思路

### team-building（团建 / 活动场地）

- **真实需求**：场地尽量大、容纳 200~300 人、交通必须便利、周边配套要求不高。
- **权重**：交通 0.5 > 面积 0.3 > 容量 0.2。交通最高，因为用户把"交通便利"当硬约束。
- **POI 类目**：`leisure`(park / sports_centre / stadium / playground / sports_hall / fitness_centre / golf_course / nature_reserve / recreation_ground)、`amenity`(conference_centre / events_venue / community_centre / theatre / arts_centre)、`tourism`(hotel / camp_site / picnic_site / theme_park / zoo / attraction)、`landuse`(recreation_ground)、`building`(stadium / sports_hall)。
- **关键词兜底**：拓展 / 团建 / 轰趴 / 农庄 / 度假 / 训练基地 / 会议 / 宴会 / 活动中心 / 乐园 / 山庄 / 生态园。

### warehouse（仓储 / 物流用地）

- **场景**：重面积与路网可达，对地铁依赖低。
- **权重**：面积 0.6 > 交通 0.2 = 容量 0.2。
- **注意**：原型阶段没有路网数据，交通维度只能用"到交通站点直线距离"近似，对仓储场景参考价值有限——真做仓储选址必须补高速口 / 国道可达性数据。

### service-outlet（服务网点）

- **场景**：重人流可达，面积要求低。
- **权重**：交通 0.6 > 面积 0.2 = 容量 0.2。
- **POI 类目**：`amenity`(community_centre / post_office / bank / library)、`shop`(mall / supermarket / convenience)、`office`(company / coworking)。

## 打分规则（三种类型通用）

- **交通分**：≤300m → 100 分；300m → `max_transit_distance` 用**平方根曲线**衰减到 0
  （`100 × (1 - √((d-300)/(max-300)))`）；无交通站点 → 0 分并标「交通未知」；
  超出硬约束另标「超出交通硬约束(Nm)」。
- **面积分**：`min(100, 100 × √(area_m2 / full_score_area))`（**开根号**缩放）；
  面积为 0 或未知 → **固定 30 分**并标「面积未知」。
- **容量分**（仅当 `target_capacity > 0` 时生效）：
  - 容量**已知**：落在 `[0.8×T, 1.6×T]` → 100；`[0.6×T, 2.2×T]` → 60；其余 → 20；
    低于 `0.8×T` 另标「容量偏小」。
  - 容量**未知**：查 `capacity_proxy` 表按场地类型推断（见下），
    并标「容量未知(按场地类型推断)」；表里也没有 → 50 分并标「容量未知」。
- **综合分** = Σ(权重 × 分项)，范围 0–100。
- **同分排序**：综合分 → 交通距离升序 → 面积降序。

### 为什么用平方根而不是线性（实测教训）

第一版用「≤300m 满分 + 线性衰减」，在广州体育西路 1500m 范围实测：
**前 10 名全部并列 90.0 分**，排名完全失去区分度——因为市中心交通点密布（742 个），
所有候选交通分都顶格 100，面积又都超过满分线。改成平方根衰减后远端拉开差距，
配合容量代理，天河体育场等真正合适的场地才浮到前面。

### 为什么需要容量代理（实测教训）

OSM 极少标注 `capacity`（实测 49 个候选里 0 个有）。若容量未知一律给中性 50 分，
「200 人的小广场」和「能装 300 人的体育场」会同分——这直接违背「容纳 200~300 人」的硬诉求。
故加 `capacity_proxy`：用场地类型做容量先验。

| 场地类型 | 代理分 | 场地类型 | 代理分 |
|---|---|---|---|
| stadium / sports_hall / conference_centre / events_venue | 85 | park / golf_course | 60 |
| theatre / arts_centre | 80 | attraction | 58 |
| sports_centre | 78 | picnic_site / nature_reserve / community_centre | 55 |
| theme_park / zoo | 70 | fitness_centre | 45 |
| hotel | 68 | playground | 40 |
| recreation_ground | 65 | camp_site | 62 |

**这是先验不是事实**，所有走代理的候选都会标「容量未知(按场地类型推断)」，必须人工核实。

### 其他设计取舍

面积为 0 的候选（OSM 里是 node，无几何）给中性 30 分而非 0 分——避免把"只有点位、
没有轮廓"的场地一棒子打死。这类项需要人工去看。

## 新增一个业务类型

复制一份配置改即可，关键是想清楚三件事：

1. 这类选址在 OSM 里对应哪些**标签**？（不确定就先手工跑一次 Overpass 看返回什么）
2. **权重**怎么排？——哪个维度是硬约束，就给哪个最高权重。
3. 中文名搜不到时，用什么**关键词**兜底？

改完记得同步更新 `SKILL.md` 里的类型表格。
