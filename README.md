# radius-dispatch · 地点范围测定 / 选址 scout

把「我有个需求 → 在哪儿合适 → 具体有哪些点可选」变成一条可复跑的流水线。

以某个点（地铁站口、地标、高铁站…）为原点按半径画范围，在范围内捞取**真实场地 POI**，
按交通便利度 / 场地类型适配度等约束筛选打分，输出候选清单 + CSV + 交互地图。

## 典型场景

- **团建 / 活动场地**：以高铁站口为原点，找能容纳 200~300 人、交通便利的场地
- **仓储 / 物流选址**：看重面积与路网可达，对地铁依赖低
- **服务网点选址**：看重人流可达

## 快速开始

纯 Python 标准库，无需 pip 安装，Windows / macOS / Linux 直接跑。

```bash
# 看内置业务类型
python scripts/radius_dispatch.py types

# 先定原点（避免地名解析跑偏）
python scripts/radius_dispatch.py geocode "体育西路地铁站" --city 广州

# 完整跑一遍（默认 OSM 数据源，免 key）
python scripts/radius_dispatch.py scout \
  --origin "体育西路地铁站" --city 广州 \
  --radius 1500 --type team-building --top 20
```

### 用高德数据（中国场地数据准得多）

申请高德 Web 服务 Key（个人认证免费，POI 搜索月配额 5,000 次）：

```bash
export AMAP_KEY=你的高德key      # 推荐环境变量，key 不落盘

python scripts/radius_dispatch.py scout \
  --origin "虎门站" --city 东莞 \
  --radius 5000 --type team-building --provider amap
```

`--provider auto` 是默认值：检测到 key 走高德，否则回退 OSM。可强制 `--provider osm` / `--provider amap`。

### 等时圈：按驾车分钟数画范围（需高德 key）

半径画「距离范围」，等时圈画「时间范围」——「虎门站出发驾车 20 分钟能到哪」：

```bash
python scripts/radius_dispatch.py isochrone \
  --origin "虎门站" --city 东莞 --minutes 20 --directions 16
```

高德无免费等时圈 API，但个人 key 可调驾车路径规划：向 N 个方向二分「恰好 N 分钟可达的最远点」，边界连成多边形。产物 `isochrone.geojson` + `isochrone-map.html`。

## 数据源

| 数据源 | 说明 | 适用 |
|---|---|---|
| `osm`（默认） | Nominatim + Overpass，免 key | 零门槛验证流程 |
| `amap` | 高德周边搜索 + 地理编码，需 key | 中国城市落地 |

坐标系差异已内置处理：OSM / 底图用 WGS-84，高德用 GCJ-02（实测偏移约 546–624 m），
代码在进出高德 API 边界自动转换，往返误差毫米级。

## 打分模型

四个维度加权（可按业务类型配置权重）：

| 维度 | 说明 |
|---|---|
| 交通 | 到最近交通站点的距离。高德下为**真实步行路网距离**；OSM 为直线距离 |
| **适配度** | 名称 + 类目信号推断「像不像这类场地」。高德不返回面积/容量，这一维是区分候选好坏的主力 |
| 面积 | 场地面积（OSM 有多边形时计算；高德不返回） |
| 容量 | 可容纳人数（两数据源均极少标注，走类型推断 + 人工核实） |

## 产物

| 文件 | 内容 |
|---|---|
| `range.geojson` | 范围圆 + 原点，可导入 QGIS / kepler.gl |
| `candidates.csv` | 候选清单（utf-8-sig，Excel 直开不乱码） |
| `candidates.json` | 全量结构化结果 |
| `report.md` | 可读清单报告 |
| `map.html` | Leaflet 交互地图：范围圆 + 候选点（按分数着色） |

## 已知限制（诚实声明）

1. **OSM 数据源下交通距离是直线距离**；高德数据源已用真实步行路网距离（`--no-walk` 可退回直线对比）
2. **适配度是先验推断**（基于名称/类目），非事实——实际容量需电话核实
3. OSM 中国场地 POI 覆盖弱，团建类场地建议用高德数据源
4. 高德个人账号有 QPS 限制，脚本内置 0.3s 全局节流 + 退避重试；超长流程仍可能偶发失败并标注

## 离线测试

```bash
python scripts/test_providers_offline.py   # 坐标转换/半径过滤/排序/字段映射，不联网不耗配额
```
