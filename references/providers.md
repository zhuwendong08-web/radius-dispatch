# 数据源与升级路径

## 当前：OpenStreetMap（免 key，原型验证用）

| 能力 | 服务 | 端点 |
|---|---|---|
| 地理编码 | Nominatim | `https://nominatim.openstreetmap.org/search` |
| POI / 交通点检索 | Overpass API | `overpass-api.de` → `kumi.systems` → `private.coffee`（依次回退） |

- **优点**：零门槛、免 API key、全球覆盖、无配额申请流程。
- **缺点**：中国场地类 POI 覆盖弱；无配额保障；公共节点偶发超时。

选型意图很明确：**先用零成本验证"画范围 → 捞 POI → 清单"这条流程对不对**，再谈数据精度。
流程错了，数据再全也是白搭。

## 目标：高德开放平台（中国落地，推荐）

### 为什么选高德不选百度

| 对比项 | 高德（个人认证） | 百度（个人认证） |
|---|---|---|
| POI 检索 | **月 5,000 次** | 日 100 次 |
| 地理编码 | 月 150,000 次 | 5,000 次/日 |
| 逆地理编码 | 月 150,000 次 | 300 次/日 |
| 路径规划 | 月 150,000 次 | 5,000 次/日 |
| 坐标系 | GCJ-02 | BD-09（需额外转一次） |

百度个人 POI 日配额只有 100 次，批量捞场地直接不够用，与高德月 5,000 次差约 **50 倍**。故选高德。

> 数据核对日期：2026-09-04。配额政策会调整，接入前请回高德开放平台控制台复核。

### 申请路径

1. 注册高德开放平台账号
2. 完成**个人认证**（身份证）——不做认证很多 API 调不了
3. 控制台 → 应用管理 → 创建新应用 → 添加 Key
4. Key 类型选 **Web 服务**（不是 JS / Android / iOS）
5. 勾选所需服务：POI 搜索、地理编码、逆地理编码、路径规划

### 接入时脚本要改的地方

**已完成**：`scripts/providers.py` 里的 `AmapProvider` 已实现以下接口，`radius_dispatch.py` 的
`--provider {auto,osm,amap}` + `--key` / `AMAP_KEY` 已接通，有 key 即用。

| 接口 | 当前实现（OSM） | 高德实现（AmapProvider） |
|---|---|---|
| `geocode(name)` | Nominatim | `AmapProvider.geocode` → `/v3/geocode/geo` |
| `search_in_radius(lat, lon, r, cats)` | Overpass | `AmapProvider.search_in_radius` → `/v3/place/around`（按 `amap_keywords` 逐词搜、分页合并去重） |
| `search_transit(lat, lon, r)` | Overpass | `AmapProvider.search_transit` → 地铁/公交/火车/高铁/汽车站周边搜索 |
| `route_distance(a, b, mode)` | **直线距离（占位实现）** | `AmapProvider.route_distance` → `/v3/direction/walking` 真实步行路网距离（**尚未接入打分，仍用直线距离**） |
| `isochrone(lat, lon, minutes)` | 未实现 | 未实现（等时圈，需公交/驾车路径规划，非步行） |

### 四个坑（均已处理，前三个经官方文档核实）

1. **坐标系必须转换。** OSM 用 WGS-84，高德用 GCJ-02。**实测偏移 546–624 米**（广州体育西路 624m、东莞虎门站 615m、北京国贸 546m），比早期估的 300–500m 更大——足以把"距地铁 500m"算成 1100m，直接改变硬约束判断。`providers.py` 已在进出高德 API 边界自动转换，迭代逆变换往返误差 0.0000m。
2. **`sortrule=distance` 在只传 keywords 时不生效。** 高德 v3 文档原文：「sortrule参数设置距离排序**在只传keywords参数的情况下不生效**」。本 skill 正是只传关键词，故**不能依赖 API 排序**。实现改为「全量取回 → 真实半径过滤 → 自己按距离排序 → 截断」，否则 `limit` 截断会丢掉近的、留下远的——而本 skill 最在意的恰恰是近的。
3. **`city` 与经纬度冲突时返回空。** 文档：「若范围内有用户指定 city 的数据则返回，**否则返回为空**」。city 名不匹配（"东莞市" vs "东莞"）会直接空结果。故 `city` 默认不传，靠 `location`+`radius` 限定。
4. **面积/容量缺字段。** 高德不返回建筑面积（`area_m2` 恒 0）与可容纳人数（`capacity` 恒 None）。这两维退化为中性分，排名主要靠交通分。

**半径上限**：周边搜索 0–50,000m，脚本已 `min(radius, 50000)` 截断。
**类目体系**：高德用 typecode（六位数字）。当前用 `amap_keywords` 中文关键词逐词搜，免维护对照表，代价是召回精度略低。

### v3 / v5 版本选择（2026-09-05 核查官方文档）

搜索 POI 文档更新 2026-07-15，地理编码与路径规划更新 2026-02-02。核查结论：

| 能力 | 采用 | 说明 |
|---|---|---|
| 地理编码 | **v3** `/v3/geocode/geo` | 官方文档主推仍 v3，无 v5 对应路径 |
| 周边搜索 | **v3** `/v3/place/around` | v3 见"搜索POI"、v5 见"搜索2.0"，**两者并存，v3 无下线迹象** |
| 步行路径规划 | **v3** `/v3/direction/walking` | v3 见"路径规划"、v5 见"路径规划2.0"，两者并存 |

**保持 v3，暂不迁移。** 关键理由：v5 周边搜索基础返回字段**不含 `tel`/`website`**（需 `show_fields=business` 才返回），v3 的 `extensions=all` 直接平铺返回，对接更简单。

若高德日后宣布 v3 下线，迁移点只有四处：
`/v3/place/around` → `/v5/place/around`、`offset`/`page` → `page_size`(1-25)/`page_num`(1-100)、
`city` → `region`、`extensions=all` → `show_fields`、返回结构 `pois[]` → `pois.poi[]`。

### 离线回归测试

`scripts/test_providers_offline.py` 覆盖坐标转换往返、半径过滤、距离排序、截断保留最近、
字段映射、配额保护四项，**不联网、不消耗配额**：

```bash
python scripts/test_providers_offline.py
```

## Mapbox（备选：按时间画范围）

`mapbox/mapbox-agent-skills` 的 `mapbox-location-grounding` 支持 **isochrone 等时圈**，能出"N 分钟可达"的多边形——正好补上"按时间而非按距离画范围"的能力。

- 需 Mapbox token
- 中国数据覆盖一般，适合做海外场景，或作为等时圈实现的参考

仓库：`https://github.com/mapbox/mapbox-agent-skills`

## 明确排除

| 方案 | 排除原因 |
|---|---|
| Google 系（google-maps-api-skill / local-places / spots） | 国内不可用或极不稳定 |
| AWS Amazon Location Service | 需 AWS 账号，国内覆盖弱 |
| CARTO（carto-site-selection） | 平台绑定重、门槛高；**但它的 pipeline 结构（已有点位+目标区→空间索引→富集→打分排序→筛 Top）是本 skill 打分设计的参考来源** |

## 关于 carto-site-selection 的借鉴

它不直接用（CARTO 商业平台依赖太重），但借鉴了它的核心结构：

```
已有/目标数据 → 空间索引 → 用人口/POI 富集 → 打分排序 → 筛 Top 候选
```

本 skill 简化为：**原点+半径定范围 → Overpass 捞 POI → 交通/面积/容量三项富化 → 加权打分 → 排序输出**。
打分思路一致，数据层换成了零门槛的 OSM。
