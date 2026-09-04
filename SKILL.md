---
name: radius-dispatch
description: 地点范围测定与选址 scout。以某个点（如地铁站口、地标）为原点按半径画连续范围，在该范围内捞取真实场地 POI，按交通便利度/面积/容量等约束筛选打分，输出候选清单、CSV、GeoJSON 与可视化地图。当用户提到"选址""找场地""团建场地""仓库选址""服务网点""地点范围""周边多少米内有什么场地""帮我画一个范围""这个范围里有哪些可选位置"等意图时使用。内置 team-building / warehouse / service-outlet 三种业务类型模板，可扩展。
---

# radius-dispatch · 地点范围测定 / 选址 scout

把「我有个需求 → 在哪儿合适 → 具体有哪些点可选」变成一条可复跑的流水线。

## 流水线

```
原点解析(地理编码) → 画范围(圆/GeoJSON) → 范围内捞真实 POI
→ 富化(交通距离/面积/容量) → 硬约束过滤 → 打分排序 → 清单 + 可视化地图
```

每一步都能单独跑、单独复查，不搞黑箱。

## 快速开始

脚本根目录：`scripts/radius_dispatch.py`（纯 Python 标准库，无需 pip 安装，Windows 直接跑）。

```bash
# 看内置业务类型
python scripts/radius_dispatch.py types

# 先把原点定准（强烈建议先做这步，避免原点跑偏）
python scripts/radius_dispatch.py geocode "体育西路地铁站" --city 广州

# 完整跑一遍（默认免 key 走 OSM）
python scripts/radius_dispatch.py scout \
  --origin "体育西路地铁站" --city 广州 \
  --radius 1500 --type team-building --top 20

# 有高德 key 时（中国落地，数据准得多）：
# 推荐用环境变量，避免 key 出现在命令行历史里
export AMAP_KEY=你的高德key
python scripts/radius_dispatch.py scout \
  --origin "体育西路地铁站" --city 广州 \
  --radius 1500 --type team-building --top 20 --provider amap
# 或者临时传：--key 你的key（脚本绝不落盘、不写进产物）
```

### scout 参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--origin` | 原点名称（地铁站/地名/地址） | — |
| `--city` | 城市名，拼进查询消歧（推荐） | — |
| `--lat/--lon` | 直接给坐标，跳过地理编码 | — |
| `--radius` | 范围半径（米） | 1500 |
| `--type` | 业务类型 | team-building |
| `--max-transit` | 交通硬约束：到最近站点最大直线距离（米） | 按类型走默认 |
| `--target-capacity` | 目标容量（人） | 按类型走默认 |
| `--top` | 清单条数 | 20 |
| `--out-dir` | 产物目录 | ./radius-dispatch-out |
| `--keep-all` | 不过滤交通不达标项（仍标注） | 关 |
| `--no-map` | 不生成 HTML 地图 | 关 |
| `--provider` | 数据源：auto / osm / amap | auto |
| `--key` | 高德 Web 服务 key；不传则读环境变量 `AMAP_KEY` | — |

### 产物

| 文件 | 内容 |
|---|---|
| `range.geojson` | 范围圆（多边形近似）+ 原点，可导入 QGIS / kepler.gl |
| `candidates.csv` | 候选清单（utf-8-sig，Excel 直开不乱码） |
| `candidates.json` | 全量结构化结果，供二次处理 |
| `report.md` | 可读清单报告，含范围设定、未通过项、已知缺陷 |
| `map.html` | Leaflet 交互地图：范围圆 + 原点 + 候选点（按分数着色） |

## 内置业务类型

| 类型 | 场景 | 权重（交通/面积/容量） | 默认硬约束 |
|---|---|---|---|
| `team-building` | 团建 / 活动场地，要大有容量、交通必须便利 | 0.5 / 0.3 / 0.2 | 交通 ≤1000m，目标 250 人 |
| `warehouse` | 仓储 / 物流用地，重面积与路网 | 0.2 / 0.6 / 0.2 | 交通 ≤5000m |
| `service-outlet` | 服务网点，重人流可达 | 0.6 / 0.2 / 0.2 | 交通 ≤800m |

扩展新类型：改 `scripts/radius_dispatch.py` 里的 `BUSINESS_TYPES` 字典——
每套配置 = OSM 标签映射 + 名称关键词兜底 + 三项权重 + 默认硬约束。详见 `references/business-types.md`。

## 使用建议

1. **先 `geocode` 定原点，再 `scout`。** Nominatim 对中文地铁站名的解析不是百分百准——
   实测「广州 体育西路地铁站」会被误判到四川凉山的「铁西」。脚本已自动清洗「地铁站/地铁/轻轨站」
   等噪声后缀，并加 `countrycodes=cn` 过滤 + 城市不匹配重罚，修完后该例已能正确命中广州体育西路。
   但解析仍非百分百可靠，**跑 scout 前先用 geocode 确认坐标**，比跑完发现偏了强。
2. **半径别一次给太大。** Overpass 在大半径下容易超时；超过 3000m 建议分段跑或直接用 `--lat/--lon` 分批。
3. **交通硬约束是可调的。** 默认 1000m 偏宽松（因为算的是直线距离）；真要严格，调到 500–800m。
4. **看 `flags` 列。** 「面积未知」「容量未知」说明 OSM 没标，这类候选的分数含中性假设，需要人工核。

## 已知缺陷（重要）

1. **交通距离是直线距离，不是步行路网距离。** 真实可达性需接路径规划 API（高德/百度/OSRM），见 `references/providers.md`。
2. **OSM 中国场地 POI 覆盖弱。** 团建常见的「拓展基地 / 轰趴馆 / 农庄 / 山庄」基本搜不到，已用名称关键词兜底但覆盖率仍低。这是要换高德/百度数据的核心原因。
3. **容量字段极少标注。** OSM 里 `capacity` 标签非常稀疏（实测 49 个候选 0 个有），
   脚本改用 `capacity_proxy` 按场地类型推断（体育场 85 / 公园 60 / 游乐场 40…），
   并标注「容量未知(按场地类型推断)」。**这是先验不是事实，必须人工核实。**
4. **依赖公共免费服务，不稳定。** Nominatim 限流约 1 次/秒（脚本已 sleep）；
   Overpass 公共节点偶发连接被截断（IncompleteRead，实测出现过），脚本已做
   「POST 逐端点 → GET 回退 → POST 重试」三轮共 9 次尝试。全挂才报错，并给出缩小半径等建议。

## 数据源与后续升级

双数据源，`--provider` 自动/手动切换，同一套打分逻辑复用：

| 数据源 | 说明 | 适用 |
|---|---|---|
| `osm`（默认） | Nominatim 地理编码 + Overpass POI，**免 key** | 零门槛验证流程 |
| `amap` | 高德周边搜索 + 地理编码，**中国场地数据准**，需 key | 中国城市落地 |

- `--provider auto`（默认）：有 key（`--key` 或 `AMAP_KEY`）走高德，否则 OSM。
- 高德 key 推荐用环境变量传，避免写进 shell 历史；脚本**绝不落盘 key**。
- 推荐**高德开放平台 Web 服务 Key**（个人认证 POI 搜索月配额 5,000，地理编码/路径规划月配额 150,000；百度个人 POI 仅 100 次/日）。
- 坐标系差异已内置处理：OSM/底图用 WGS-84，高德用 GCJ-02（实测偏移约 550–625m），
  `providers.py` 在进出高德 API 边界自动做转换，往返精度毫米级。

**高德数据源的两个固有限制**（非 bug，是 API 不返回这些字段）：
1. 高德周边搜索**不返回建筑面积**，候选 `area_m2` 恒为 0 → 面积维走「面积未知」中性分。
2. 高德**不返回可容纳人数**，容量维走「容量未知」中性分。
   这两维在高德下会退化为中性分，**排名主要靠交通分**——所以「交通必须便利」这个硬约束在高德下反而更纯粹。

详见 `references/providers.md`。
