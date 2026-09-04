# 业务类型配置

**真源在 `config/business_types.json`**（不在代码里）。脚本启动时加载内置配置；
加 `--type-file <json>` 可覆盖/扩展（同 key 覆盖、新 key 新增），**用户不碰代码即可加新类型**。

每种业务类型 = 一套「约束模板 + POI 类目映射 + 打分权重」。

## 配置结构（JSON）

```json
"<type-key>": {
  "label": "中文名",
  "summary": "一句话场景说明，会原样写进 report.md",
  "osm_tags": {"<osm-key>": ["<value>", ...]},
  "name_keywords": ["关键词", ...],
  "amap_keywords": ["高德关键词", ...],
  "weights": {"transit": 0.3, "suitability": 0.5, "size": 0.1, "capacity": 0.1},
  "suitability": {
    "small_venue": ["小容量业态词", ...], "small_venue_score": 55,
    "strong": {"名称强意图词": 95, ...},
    "medium": {"中等意图词": 85, ...},
    "weak": {"容量不足词": 30, ...},
    "category": {"高德中文类目": 分, ...},
    "noise": ["噪声词", ...], "noise_score": 12,
    "default": 50
  },
  "defaults": {
    "max_transit_distance": 1000, "target_capacity": 250,
    "full_score_area": 5000
  }
}
```

权重是**四维**：`transit`(交通) / `suitability`(适配度) / `size`(面积) / `capacity`(容量)。
高德下 size/capacity 恒走中性分（无数据），**suitability 是区分主力**，一般给最高权重。

## 三个内置类型的设计思路

### team-building（团建 / 活动场地）

- **真实需求**：场地尽量大、容纳 200~300 人、交通必须便利、周边配套要求不高。
- **权重**：适配度 0.5 > 交通 0.3 > 面积 0.1 = 容量 0.1。
- **POI 类目（OSM）**：leisure(park/sports_centre/stadium/…)、amenity(conference_centre/events_venue/…)、tourism(hotel/camp_site/…)。
- **高德关键词**：拓展基地 / 团建 / 轰趴馆 / 农庄 / 度假村 / 会议中心 / 山庄 / 庄园 / 农场 / 营地 / 真人CS / 烧烤…（不含「培训基地」——实测召回 80 个培训机构）。
- **适配度信号**（见 JSON）：small_venue 压制手作/电玩馆（市中心实测教训）；
  烧烤/采摘/钓虾是「营地项目」非场地本身，单出给低分（虎门实测教训）。

### warehouse（仓储 / 物流用地）

- **场景**：重面积与路网可达，对地铁依赖低。
- **权重**：适配度 0.45 > 交通 0.25 > 面积 0.2 > 容量 0.1。
  （原 size 0.6 为 OSM 面积设计，高德无面积 → 实测前 12 名全 52.0 分饱和，已调低。）
- **适配度**：园区级(物流园/产业园/仓储中心) > 单体仓库。业务词（纺织/机械）不降分。
- **诚实局限**：无面积数据无法区分单体仓库大小；「交通便利」真义是高速/国道可达，当前公交距离是近似。

### service-outlet（服务网点）

- **场景**：重人流可达，面积要求低。
- **权重**：适配度 0.3 > 面积 0.1 = 容量 0.1（transit 0.5 最高，网点看人流可达）。
- **POI 类目（OSM）**：amenity(community_centre/post_office/bank/library)、shop(mall/supermarket/convenience)、office(company/coworking)。

## 打分规则

- **交通分**：≤300m → 100 分；300m → `max_transit_distance` 用**平方根曲线**衰减到 0
  （`100 × (1 - √((d-300)/(max-300)))`）；无交通站点 → 0 分并标「交通未知」；
  超出硬约束另标「超出交通硬约束(Nm)」。高德下为**真实步行路网距离**（top-3 站点取优），OSM 下为直线。
- **适配度分**（2026-09-05 新增第四维，见下文）：名称 + 类目信号判断「像不像这类场地」。
- **面积分**：`min(100, 100 × √(area_m2 / full_score_area))`（**开根号**缩放）；
  面积为 0 或未知（高德恒无）→ **固定 30 分**并标「面积未知」。
- **容量分**（仅当 `target_capacity > 0` 时生效）：
  - 容量**已知**：落在 `[0.8×T, 1.6×T]` → 100；`[0.6×T, 2.2×T]` → 60；其余 → 20；
    低于 `0.8×T` 另标「容量偏小」。
  - 容量**未知**：查 `capacity_proxy` 表按场地类型推断（见下），
    并标「容量未知(按场地类型推断)」；表里也没有 → 50 分并标「容量未知」。
- **综合分** = Σ(权重 × 分项)，范围 0–100。
- **同分排序**：综合分 → 交通距离升序 → 面积降序。

### 适配度维度与信号优先级（为什么需要它）

高德不返回面积/容量，那两维恒为常数（实测所有交通达标候选分数全并列，星巴克和户外拓展
同分）。适配度接住高德的中文类目 + 名称信号来区分候选。信号优先级：

**噪声词 > 小容量业态(small_venue) > 名称强意图 > 名称中等意图 > 负向(容量不足 weak) > 类目 > 默认**

- 噪声词：命中直接判非目标（「XX小吃实训」哪怕像培训机构）。
- small_venue：手作/电玩/剧本杀等体验馆，名字标了「团建/轰趴」也装不下大团（市中心实测教训）。
- weak：球场/篮球/游泳/健身——**容量不符**非用途不符，无此层会霸占前排挤掉真农庄（实测教训）。

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

**不用改代码**：复制 `config/business_types.json` 里任一段配置改即可，关键想清楚三件事：

1. 这类选址在高德里用什么**中文关键词**搜？（对应 `amap_keywords`；不确定先跑一次周边搜看返回什么）
2. **适配度词表**要覆盖这类业态的**真实命名习惯**（如摄影类要含「照相馆/婚纱摄影」，只写「摄影棚」会漏）。
3. **权重**怎么排？——哪个维度是硬约束就给哪个最高；高德无面积/容量，适配度通常给最高。

用 `--type-file` 加载（同 key 覆盖内置、新 key 新增）：

```bash
python scripts/radius_dispatch.py --type-file my_types.json types          # 看合并后有哪些类型
python scripts/radius_dispatch.py --type-file my_types.json scout --origin ... --type my-new-type ...
```

内置三类配置若要调整，直接改 `config/business_types.json`（已入库，随仓库分发）。
改完记得同步更新 `SKILL.md` 里的类型表格。

**示例：自定义「摄影棚/影楼选址」**（最小配置）：

```json
{"photo-studio": {
  "label": "摄影棚 / 影楼选址",
  "summary": "要层高与面积，交通次之",
  "osm_tags": {"building": ["commercial"], "shop": ["photo", "art"]},
  "name_keywords": ["摄影棚", "影楼", "摄影基地"],
  "amap_keywords": ["摄影棚", "影楼", "摄影基地", "照相馆", "婚纱摄影"],
  "weights": {"transit": 0.2, "suitability": 0.5, "size": 0.2, "capacity": 0.1},
  "suitability": {
    "small_venue": [],
    "strong": {"摄影棚": 95, "摄影基地": 95, "影楼": 92, "照相馆": 90},
    "medium": {"摄影": 85, "写真": 82, "婚纱": 85},
    "weak": {},
    "category": {"摄影冲印": 80},
    "noise": ["培训", "食堂", "网吧"],
    "noise_score": 15,
    "default": 50
  },
  "defaults": {"max_transit_distance": 1500, "target_capacity": 0, "full_score_area": 500}
}}
```
