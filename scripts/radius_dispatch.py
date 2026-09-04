#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
radius-dispatch · 地点范围测定 / 选址 scout
=====================================

流水线：原点解析 → 画范围(圆) → 范围内捞真实 POI → 富化(交通/面积/容量) → 硬约束过滤 → 打分排序 → 出清单 + 可视化地图

数据源：OpenStreetMap（Nominatim 地理编码 + Overpass POI 检索）
    —— 免 API key，原型阶段零门槛。中国落地需换高德/百度，见 references/providers.md

子命令
------
  types                                            列出内置业务类型及其约束模板
  geocode  "体育西路地铁站" [--city 广州]           地名/地铁站名 → 坐标
  scout    --origin "..." --radius 1500 [...]      完整流水线

scout 常用参数
--------------
  --origin NAME        原点名称（地铁站名/地名/地址）；与 --lat/--lon 二选一
  --city CITY          城市名，拼到原点查询里消歧（推荐，如「广州」）
  --lat / --lon        直接给原点坐标，跳过地理编码
  --radius M           范围半径（米）。默认 1500
  --type TYPE          业务类型：team-building / warehouse / service-outlet。默认 team-building
  --max-transit M      交通硬约束：候选点到最近交通站点的最大直线距离（米）。默认 1000
  --target-capacity N  目标容量（人）。默认 250
  --top N              清单输出前 N 条。默认 20
  --out-dir DIR        产物输出目录。默认 ./radius-dispatch-out
  --keep-all           不过滤不满足交通硬约束的候选（仍会标注），用于看全量
  --no-map             跳过 HTML 地图生成

示例（Windows / Git Bash 均可）
------------------------------
  python radius_dispatch.py types
  python radius_dispatch.py geocode "体育西路地铁站" --city 广州
  python radius_dispatch.py scout --origin "体育西路地铁站" --city 广州 --radius 1500 --type team-building --top 20

已知缺陷（原型阶段）
--------------------
  1. 交通距离是「直线距离」，不是步行路网距离。真实可达性需接路径规划 API（见 providers.md）。
  2. OSM 中国场地类 POI 覆盖弱：团建常见的「拓展基地 / 轰趴馆 / 农庄」大概率搜不到，
     已用 name 关键词兜底，但覆盖率仍有限。这是换高德/百度的主要原因。
  3. 容量(capacity)在 OSM 里极少标注，容量维度多数候选项会标「未知」并按中性分处理。
  4. Nominatim 限流约 1 次/秒，脚本已内置 sleep；Overpass 公共节点偶发超时，已配多端点回退。
"""

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

# 让 radius_dispatch.py 能 import 同目录的 providers.py（无论从哪启动）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from providers import AmapProvider
except ImportError:  # providers.py 缺失时仍可跑纯 OSM 流程
    AmapProvider = None

# Windows 控制台中文输出
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

UA = "radius-dispatch/0.1 (local WorkBuddy skill; python-urllib)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
EARTH_R = 6371000.0

# ----------------------------------------------------------------------------
# 业务类型配置：约束模板 + POI 类目映射 + 打分权重
# 每套配置就是「业务类型参数化」的落点，改这里即可扩展新类型
# ----------------------------------------------------------------------------
BUSINESS_TYPES = {
    "team-building": {
        "label": "团建 / 活动场地",
        "summary": "要场地大、能容纳 200~300 人、交通必须便利；周边配套要求不高。",
        "osm_tags": {
            "leisure": ["park", "sports_centre", "stadium", "playground", "sports_hall",
                        "fitness_centre", "golf_course", "nature_reserve", "recreation_ground"],
            "amenity": ["conference_centre", "events_venue", "community_centre",
                        "theatre", "arts_centre"],
            "tourism": ["hotel", "camp_site", "picnic_site", "theme_park", "zoo",
                        "attraction"],
            "landuse": ["recreation_ground"],
            "building": ["stadium", "sports_hall"],
        },
        "name_keywords": ["拓展", "团建", "轰趴", "农庄", "度假", "训练基地",
                          "会议", "宴会", "活动中心", "乐园", "山庄", "生态园"],
        # 权重四维度。适配度 0.5 高于交通 0.3，理由：
        #   场地类型错了（篮球场装不下 200 人），离地铁再近也没用；
        #   反之场地对了，远 500 米可以包车解决。实测若两者同权，
        #   交通顶格的球场会压过交通一般的户外拓展基地，排序失去业务意义。
        "weights": {"transit": 0.3, "suitability": 0.5, "size": 0.1,
                    "capacity": 0.1},
        # ---- 适配度：这个场地「像不像」团建场地 ----
        # 高德不返回面积/容量，但返回中文类目和名称，这两者区分度极高：
        #   「小满营地烧烤.采摘.团建」这种名字比任何类型推断都准。
        # 名称信号优先于类目信号（名字直接写用途 > 平台归档的类目）。
        "suitability": {
            # 强意图：名字里直接写了就是干这个的
            "strong": {"团建": 95, "拓展": 95, "拓展基地": 98, "轰趴": 90,
                       "真人CS": 92, "CS": 90, "营地": 88, "露营": 85,
                       "烧烤": 82, "采摘": 80, "户外": 78},
            # 中等意图：场地类型对（有大片空地，能装下 200 人），但没明说用途
            "medium": {"生态园": 88, "农庄": 85, "山庄": 85, "农家乐": 85,
                       "庄园": 82, "农场": 82, "度假": 80, "度假村": 82,
                       "乐园": 72, "会议中心": 78, "宴会厅": 70, "活动中心": 70},
            # 负向：是运动/休闲场地，但装不下 200~300 人（容量不符，非用途不符）
            #   实测这批是最大的"高分噪声"——篮球场/游泳馆/健身中心离地铁近，
            #   交通分顶格，若无此层会霸占前排，把真正的农庄挤下去。
            "weak": {"球场": 30, "篮球": 25, "网球": 25, "足球": 30,
                     "羽毛球": 30, "乒乓球": 25, "游泳": 30, "健身": 25,
                     "台球": 25, "瑜伽": 25, "舞蹈": 30, "跆拳道": 25,
                     "武馆": 30, "溜冰": 25},
            # 类目兜底（高德中文类目）。数值同样反映容量，不只看用途。
            "category": {"综合体育馆": 80, "会展中心": 85, "展览馆": 80,
                         "体育休闲服务场所": 70, "运动场所": 65, "休闲场所": 62,
                         "旅游景点": 65, "公园": 62, "度假疗养场所": 76,
                         "篮球场馆": 25, "网球场": 25, "游泳馆": 30,
                         "健身中心": 25, "乒乓球馆": 25, "台球厅": 25,
                         "培训机构": 10, "科教文化场所": 30,
                         "中餐厅": 30, "火锅店": 25, "特色/地方风味餐厅": 30,
                         "湖南菜(湘菜)": 25, "咖啡厅": 20, "星巴克咖啡": 15,
                         "冷饮店": 10, "生活服务场所": 25, "小吃": 15,
                         "购物相关场所": 10, "服装鞋帽皮具店": 10,
                         "公司企业": 15, "公司": 15, "驾校": 10,
                         "美容美发店": 10, "医疗保健服务场所": 15},
            # 噪声词：出现即判定非目标场地（优先级最高）
            "noise": ["小吃", "实训", "培训", "卷边", "电工", "焊工", "叉车",
                      "起重机", "电梯", "安全管理", "驾校", "美容", "理发",
                      "纺织", "潜水料", "广告", "装饰"],
            "noise_score": 12,
            "default": 50,
        },
        # 容量代理：OSM 极少标注 capacity，故用「场地类型」推断可容纳能力，
        # 避免把「200 人的小广场」和「能装 300 人的体育场」判成同分。
        "capacity_proxy": {
            "stadium": 85, "sports_hall": 85, "conference_centre": 85,
            "events_venue": 85, "theatre": 80, "arts_centre": 80,
            "sports_centre": 78, "theme_park": 70, "zoo": 70,
            "recreation_ground": 65, "hotel": 68, "camp_site": 62,
            "park": 60, "golf_course": 60, "attraction": 58,
            "picnic_site": 55, "nature_reserve": 55, "community_centre": 55,
            "fitness_centre": 45, "playground": 40,
        },
        # 高德关键词。
        # 「培训基地」已删除：实测它召回 80 个培训机构（叉车/电工/小吃实训/艺术培训），
        #   占候选总量 58%，是最大的噪声源。团建要的是「拓展培训」，不是「叉车培训」。
        # 「体育馆」「体育中心」也已删除：召回的多是篮球场/游泳馆/健身房，
        #   这类场地装不下 200~300 人，属容量不符而非用途不符。
        "amap_keywords": ["拓展基地", "团建", "轰趴馆", "农家乐", "农庄", "度假村",
                          "会议中心", "会展中心", "宴会厅", "生态园", "户外拓展",
                          "山庄", "庄园", "农场", "生态园", "露营", "营地",
                          "真人CS", "采摘", "烧烤"],
        "defaults": {"max_transit_distance": 1000, "target_capacity": 250,
                     "full_score_area": 5000},
    },
    "warehouse": {
        "label": "仓储 / 物流用地",
        "summary": "重面积与路网可达，对地铁依赖低；原型阶段只能看面积 + 交通点距离。",
        "osm_tags": {
            "landuse": ["industrial"],
            "building": ["warehouse", "industrial"],
            "industrial": ["warehouse"],
        },
        "name_keywords": ["仓库", "物流", "产业园", "仓储", "货运", "配送中心"],
        "amap_keywords": ["仓库", "物流园", "产业园", "仓储中心", "物流中心", "货运站"],
        "weights": {"transit": 0.2, "suitability": 0.1, "size": 0.6,
                    "capacity": 0.1},
        "suitability": {
            "strong": {"仓库": 90, "仓储中心": 95, "物流园": 92, "物流中心": 92,
                       "产业园": 85, "配送中心": 88, "货运站": 85},
            "medium": {"工业": 70, "厂房": 75, "基地": 65},
            "category": {"仓储": 85, "物流速递": 85, "工厂": 70, "工业园区": 82},
            "noise": [], "noise_score": 20, "default": 50,
        },
        "defaults": {"max_transit_distance": 5000, "target_capacity": 0,
                     "full_score_area": 20000},
    },
    "service-outlet": {
        "label": "服务网点",
        "summary": "重人流可达，交通分权重最高；面积要求低。",
        "osm_tags": {
            "amenity": ["community_centre", "post_office", "bank", "library"],
            "shop": ["mall", "supermarket", "convenience"],
            "office": ["company", "coworking"],
        },
        "name_keywords": ["服务中心", "营业厅", "网点", "便民", "驿站"],
        "amap_keywords": ["服务中心", "营业厅", "网点", "便民服务中心", "社区服务中心"],
        "weights": {"transit": 0.5, "suitability": 0.3, "size": 0.1,
                    "capacity": 0.1},
        "suitability": {
            "strong": {"服务中心": 90, "营业厅": 92, "便民服务中心": 92,
                       "社区服务中心": 90, "网点": 88, "驿站": 85},
            "medium": {"社区": 70, "办事": 72, "政务": 72},
            "category": {"生活服务场所": 75, "便民服务": 80, "政府机构": 60},
            "noise": [], "noise_score": 20, "default": 50,
        },
        "defaults": {"max_transit_distance": 800, "target_capacity": 0,
                     "full_score_area": 1000},
    },
}

CAPACITY_KEYS = ["capacity", "capacity:persons", "seats", "max_capacity"]

# OSM 里大量场地没有 name 标签，用类型生成可读占位名，避免清单里满屏「(未命名)」
KIND_CN = {
    "park": "公园", "stadium": "体育场", "sports_centre": "体育中心",
    "sports_hall": "体育馆", "playground": "游乐场", "recreation_ground": "休闲场地",
    "fitness_centre": "健身中心", "golf_course": "高尔夫球场", "nature_reserve": "自然保护区",
    "conference_centre": "会议中心", "events_venue": "活动场馆", "community_centre": "社区中心",
    "theatre": "剧院", "arts_centre": "艺术中心", "hotel": "酒店",
    "camp_site": "露营地", "picnic_site": "野餐点", "theme_park": "主题公园",
    "zoo": "动物园", "attraction": "景点", "mall": "商场", "supermarket": "超市",
    "convenience": "便利店", "industrial": "工业用地", "warehouse": "仓库",
    "post_office": "邮局", "bank": "银行", "library": "图书馆",
}
KIND_KEYS = ("leisure", "amenity", "tourism", "landuse", "building",
             "shop", "office", "industrial")


def fallback_name(tags):
    """按类型给无名要素一个可读名字，如『未命名·公园』"""
    for k in KIND_KEYS:
        if k in tags:
            v = tags[k]
            return f"未命名·{KIND_CN.get(v, v)}"
    return "(未命名)"


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2):
    """两点球面距离（米）"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def polygon_area_m2(coords):
    """用等距圆柱投影近似计算多边形面积（平方米）。coords: [(lat, lon), ...]"""
    if not coords or len(coords) < 3:
        return 0.0
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    lat0 = sum(lats) / len(lats)
    kx = math.pi / 180.0 * EARTH_R * math.cos(math.radians(lat0))
    ky = math.pi / 180.0 * EARTH_R
    pts = [(lons[i] * kx, lats[i] * ky) for i in range(len(coords))]
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def http_post_json(url, data, timeout=180):
    req = urllib.request.Request(
        url, data=data.encode("utf-8"),
        headers={"User-Agent": UA, "Content-Type": "text/plain; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_get_json(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ----------------------------------------------------------------------------
# 1) 原点解析：地名 / 地铁站名 → 坐标（Nominatim）
# ----------------------------------------------------------------------------
# Nominatim 对「XX地铁站」这类中文写法很敏感：实测「广州 体育西路地铁站」会被误判到
# 四川凉山的「铁西」，而「体育西路」或「体育西路站」都能正确命中广州。故先清洗噪声后缀。
NOISE_SUFFIXES = ["地铁站", "地铁", "轻轨站", "轨道交通站", "城铁站"]


def clean_place_name(name):
    """去掉地铁类噪声后缀：「体育西路地铁站」→「体育西路」。不改变「广州站」这类写法。"""
    n = name.strip()
    for suf in NOISE_SUFFIXES:
        if n.endswith(suf) and len(n) > len(suf):
            return n[: -len(suf)].strip()
    return n


def _nominatim_search(qtext, country, limit):
    params = {
        "format": "jsonv2",
        "limit": str(limit),
        "q": qtext,
        "accept-language": "zh",
        "addressdetails": "1",
    }
    if country:
        params["countrycodes"] = country
    url = f"{NOMINATIM_URL}?{urllib.parse.urlencode(params)}"
    data = http_get_json(url)
    out = []
    for it in data:
        try:
            out.append({
                "name": it.get("display_name", ""),
                "lat": float(it["lat"]),
                "lon": float(it["lon"]),
                "type": it.get("type") or it.get("class") or "",
                "importance": float(it.get("importance") or 0),
                "query_used": qtext,
            })
        except Exception:
            continue
    time.sleep(1.1)  # Nominatim 限流约 1 次/秒
    return out


def geocode(query, city=None, country="cn", limit=5):
    """
    多策略地理编码：
      1) 城市 + 清洗名    2) 城市 + 原名    3) 清洗名    4) 原名
    汇总后按「站点加权 + 城市匹配 + 相关度」排序，取最优。
    """
    cleaned = clean_place_name(query)
    variants = []
    if city:
        if cleaned != query:
            variants.append(f"{city} {cleaned}")
        variants.append(f"{city} {query}")
    if cleaned != query:
        variants.append(cleaned)
    variants.append(query)

    seen_q, queries = set(), []
    for v in variants:
        if v not in seen_q:
            seen_q.add(v)
            queries.append(v)

    results, seen_key = [], set()
    for qtext in queries:
        try:
            hits = _nominatim_search(qtext, country, limit)
        except Exception:
            continue
        for h in hits:
            key = (round(h["lat"], 5), round(h["lon"], 5), h["name"])
            if key in seen_key:
                continue
            seen_key.add(key)
            results.append(h)

    if not results:
        return []

    want_station = ("站" in query) or ("地铁" in query)

    def rank(h):
        s = h["importance"]
        if want_station:
            if "出入口" in h["name"]:
                s += 0.50        # 地铁出入口，最贴近「以地铁站口为原点」
            if h["type"] in ("station", "subway_entrance", "halt"):
                s += 0.40
            if h["name"].endswith("站") or "地铁站" in h["name"]:
                s += 0.20
        if city and city in h["name"]:
            s += 0.15
        elif city:
            s -= 1.00            # 城市对不上（如被误判到外省），重罚
        return s

    results.sort(key=rank, reverse=True)
    return results


# ----------------------------------------------------------------------------
# 2) Overpass 检索
# ----------------------------------------------------------------------------
def overpass(query, timeout=180):
    """
    公共 Overpass 节点不稳定，常出现连接被提前关闭（IncompleteRead）导致响应截断。
    故采用多轮策略：第一轮 POST 逐端点 → 第二轮改用 GET（对代理/网关更友好）→ 第三轮 POST 重试。
    """
    last_err = None
    plans = [("post", 0), ("get", 0), ("post", 1)]
    for method, rnd in plans:
        for ep in OVERPASS_ENDPOINTS:
            try:
                if method == "post":
                    return http_post_json(ep, query, timeout=timeout)
                url = f"{ep}?data={urllib.parse.quote(query)}"
                return http_get_json(url, timeout=timeout)
            except Exception as e:
                last_err = e
                time.sleep(1.0 + rnd)
        time.sleep(1.0 + rnd)
    raise RuntimeError(
        f"所有 Overpass 端点均失败（已 POST/GET 多轮重试），最后错误：{last_err}")


def build_candidate_query(cfg, lat, lon, radius):
    """按业务类型配置拼 Overpass QL：标签匹配 + 名称关键词兜底"""
    lines = []
    for key, vals in cfg["osm_tags"].items():
        regex = "|".join(vals)
        lines.append(f'  nwr(around:{int(radius)},{lat},{lon})["{key}"~"^({regex})$"];')
    for kw in cfg.get("name_keywords", []):
        lines.append(f'  nwr(around:{int(radius)},{lat},{lon})["name"~"{kw}"];')
    body = "\n".join(lines)
    return f"[out:json][timeout:120];\n(\n{body}\n);\nout tags geom;"


def build_transit_query(lat, lon, radius):
    return f"""[out:json][timeout:120];
(
  nwr(around:{int(radius)},{lat},{lon})["railway"~"^(station|subway_entrance|halt)$"];
  nwr(around:{int(radius)},{lat},{lon})["public_transport"~"^(station|stop_position|platform)$"];
  nwr(around:{int(radius)},{lat},{lon})["amenity"="bus_station"];
  nwr(around:{int(radius)},{lat},{lon})["highway"="bus_stop"];
);
out tags center;"""


def parse_elements(elements):
    """把 Overpass 原始要素解析成统一候选结构"""
    out = []
    for el in elements:
        tags = el.get("tags", {}) or {}
        etype = el.get("type")
        geom = None
        if etype == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            if el.get("geometry"):
                lat = sum(g["lat"] for g in el["geometry"]) / len(el["geometry"])
                lon = sum(g["lon"] for g in el["geometry"]) / len(el["geometry"])
                geom = [(g["lat"], g["lon"]) for g in el["geometry"]]
            elif el.get("center"):
                lat, lon = el["center"]["lat"], el["center"]["lon"]
            else:
                continue
        if lat is None or lon is None:
            continue
        name = (tags.get("name") or tags.get("name:zh")
                or tags.get("official_name") or "")
        if not name:
            name = fallback_name(tags)
        # 命中来源：哪个标签对上了
        kind = ""
        for k in KIND_KEYS:
            if k in tags:
                kind = f"{k}={tags[k]}"
                break
        if not kind and tags:
            kind = ",".join(list(tags.keys())[:2])
        # 容量
        capacity = None
        for ck in CAPACITY_KEYS:
            if ck in tags:
                try:
                    capacity = int(float(str(tags[ck]).split(";")[0].strip()))
                    break
                except Exception:
                    continue
        area = polygon_area_m2(geom) if geom else 0.0
        out.append({
            "id": f"{etype}/{el.get('id')}",
            "name": name,
            "lat": lat,
            "lon": lon,
            "kind": kind,
            "area_m2": round(area, 1),
            "capacity": capacity,
            "phone": tags.get("phone") or tags.get("contact:phone") or "",
            "website": tags.get("website") or tags.get("contact:website") or "",
            "address": tags.get("addr:full") or tags.get("addr:street") or "",
            "tags": tags,
        })
    # 去重（同名同坐标）
    seen, uniq = set(), []
    for c in out:
        key = (c["name"], round(c["lat"], 5), round(c["lon"], 5))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def parse_transit(elements):
    out = []
    for el in elements:
        tags = el.get("tags", {}) or {}
        if el.get("type") == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            continue
        name = tags.get("name") or tags.get("name:zh") or "(未命名站点)"
        ttype = (tags.get("railway") or tags.get("public_transport")
                 or tags.get("amenity") or tags.get("highway") or "")
        out.append({"name": name, "lat": lat, "lon": lon, "type": ttype})
    return out


# ----------------------------------------------------------------------------
# 3) 富化 / 打分
# ----------------------------------------------------------------------------
def enrich(cands, transits, origin):
    """补：到原点距离、最近交通站点及距离"""
    for c in cands:
        c["dist_to_origin_m"] = round(haversine(origin["lat"], origin["lon"],
                                                c["lat"], c["lon"]), 1)
        best = None
        for t in transits:
            d = haversine(c["lat"], c["lon"], t["lat"], t["lon"])
            if best is None or d < best[1]:
                best = (t["name"], d, t["type"])
        if best:
            c["nearest_transit"] = best[0]
            c["transit_dist_m"] = round(best[1], 1)
            c["nearest_transit_type"] = best[2]
        else:
            c["nearest_transit"] = ""
            c["transit_dist_m"] = None
            c["nearest_transit_type"] = ""
    return cands


def capacity_proxy(proxy_map, kind):
    """
    用「场地类型」推断可容纳能力。kind 形如 'leisure=stadium'。
    OSM 极少标注 capacity，全靠这层先验把「200 人的小广场」和「能装 300 人的体育场」拉开差距。
    """
    if not proxy_map or not kind:
        return None
    val = kind.split("=", 1)[1] if "=" in kind else kind
    return proxy_map.get(val)


def suitability_score(cfg_suit, name, kind):
    """
    场地「像不像」目标业务的场地。0-100。

    为什么需要这一维：高德不返回面积/容量，那两维恒为常数
    （实测所有交通达标候选分数全并列，星巴克和户外拓展公司同分）。
    但高德返回中文类目和名称，区分度极高。

    信号优先级：名称强意图 > 名称中等意图 > 噪声词 > 类目 > 默认。
    名字里直接写用途（如「小满营地烧烤.采摘.团建」）比平台归档的类目更可信。
    """
    if not cfg_suit:
        return 50.0, []
    text = f"{name or ''} {kind or ''}"
    # 1) 噪声词优先判定：命中噪声直接给低分，不再看其他信号
    #    （如「XX小吃实训机构」虽含「机构」，但明显不是团建场地）
    for w in cfg_suit.get("noise", []):
        if w in text:
            return float(cfg_suit.get("noise_score", 12)), [f"疑似非目标场地(含「{w}」)"]
    # 2) 名称强意图：取所有命中里的最高分
    hits = [(w, s) for w, s in cfg_suit.get("strong", {}).items() if w in text]
    if hits:
        best = max(hits, key=lambda x: x[1])
        return float(best[1]), [f"名称命中「{best[0]}」"]
    # 3) 名称中等意图
    hits = [(w, s) for w, s in cfg_suit.get("medium", {}).items() if w in text]
    if hits:
        best = max(hits, key=lambda x: x[1])
        return float(best[1]), [f"名称含「{best[0]}」"]
    # 4) 负向：是运动场地但容量不够（如篮球场/游泳馆），明显不是团建目的地
    hits = [(w, s) for w, s in cfg_suit.get("weak", {}).items() if w in text]
    if hits:
        best = max(hits, key=lambda x: x[1])
        return float(best[1]), [f"容量可能不足(「{best[0]}」类小场地)"]
    # 5) 类目兜底
    cat = cfg_suit.get("category", {})
    if kind and kind in cat:
        return float(cat[kind]), [f"按类目「{kind}」推断"]
    return float(cfg_suit.get("default", 50)), ["用途未知"]


def score_candidates(cands, cfg, max_transit, target_capacity, full_score_area):
    w = cfg["weights"]
    for c in cands:
        flags = []
        # 交通分：<=300m 满分（300m 内到站点，对团建算「真便利」）；
        #   300m→max_transit 用平方根曲线衰减（近端降得慢、远端降得快）
        if c["transit_dist_m"] is None:
            transit_s = 0.0
            flags.append("交通未知")
        else:
            d = c["transit_dist_m"]
            if d <= 300:
                transit_s = 100.0
            elif d >= max_transit:
                transit_s = 0.0
            else:
                span = max(max_transit - 300, 1)
                transit_s = 100.0 * (1.0 - math.sqrt((d - 300) / span))
            if d > max_transit:
                flags.append(f"超出交通硬约束({max_transit}m)")
        # 面积分：开根号缩放（同理，线性会让城市里大片场地全部顶格）
        if c["area_m2"] and c["area_m2"] > 0:
            size_s = min(100.0, 100.0 * math.sqrt(c["area_m2"] / max(full_score_area, 1)))
        else:
            size_s = 30.0
            flags.append("面积未知")
        # 容量分：落在目标区间满分，未知给中性
        if c["capacity"]:
            cap = c["capacity"]
            if target_capacity and target_capacity > 0:
                lo, hi = target_capacity * 0.8, target_capacity * 1.6
                if lo <= cap <= hi:
                    cap_s = 100.0
                elif target_capacity * 0.6 <= cap <= target_capacity * 2.2:
                    cap_s = 60.0
                else:
                    cap_s = 20.0
                if cap < target_capacity * 0.8:
                    flags.append(f"容量偏小({cap}人)")
            else:
                cap_s = 60.0
        else:
            proxy = capacity_proxy(cfg.get("capacity_proxy"), c["kind"])
            if proxy is not None:
                cap_s = float(proxy)
                flags.append("容量未知(按场地类型推断)")
            else:
                cap_s = 50.0
                flags.append("容量未知")
        # 适配度：高德不返回面积/容量，这一维是区分候选好坏的主力
        suit_s, suit_flags = suitability_score(
            cfg.get("suitability"), c.get("name", ""), c.get("kind", ""))
        flags.extend(suit_flags)

        w_suit = w.get("suitability", 0.0)
        total = (w["transit"] * transit_s + w_suit * suit_s
                 + w["size"] * size_s + w["capacity"] * cap_s)
        c["score"] = round(total, 1)
        c["score_parts"] = {"transit": round(transit_s, 1),
                            "suitability": round(suit_s, 1),
                            "size": round(size_s, 1),
                            "capacity": round(cap_s, 1)}
        c["flags"] = flags
    return cands


# ----------------------------------------------------------------------------
# 4) 产物输出
# ----------------------------------------------------------------------------
FIELDS = ["rank", "name", "score", "kind", "dist_to_origin_m", "transit_dist_m",
          "nearest_transit", "area_m2", "capacity", "address", "phone",
          "website", "lat", "lon", "id"]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_report(path, meta, rows, filtered_out):
    lines = []
    lines.append("# 选址候选清单 · radius-dispatch\n")
    lines.append("## 范围设定\n")
    lines.append(f"- 原点：**{meta['origin_name']}**（{meta['lat']:.6f}, {meta['lon']:.6f}）")
    lines.append(f"- 半径：**{meta['radius']} m**（圆形范围，已输出 GeoJSON / 地图）")
    lines.append(f"- 业务类型：**{meta['type_label']}** — {meta['type_summary']}")
    lines.append(f"- 交通硬约束：候选点到最近交通站点 ≤ **{meta['max_transit']} m**（直线距离）")
    if meta["target_capacity"]:
        lines.append(f"- 目标容量：**{meta['target_capacity']} 人**")
    lines.append(f"- 检索到候选 **{meta['total']}** 个，通过硬约束 **{meta['passed']}** 个\n")
    lines.append("## 候选清单（按综合分降序）\n")
    lines.append("| # | 名称 | 综合分 | 类型 | 距原点(m) | 最近交通(m) | 站点 | 面积(m²) | 容量 | 备注 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        area = r["area_m2"] if r["area_m2"] else "—"
        cap = r["capacity"] if r["capacity"] else "—"
        td = r["transit_dist_m"] if r["transit_dist_m"] is not None else "—"
        flags = "；".join(r["flags"]) if r["flags"] else ""
        lines.append(f"| {r['rank']} | {r['name']} | {r['score']} | {r['kind']} | "
                     f"{r['dist_to_origin_m']} | {td} | {r['nearest_transit']} | "
                     f"{area} | {cap} | {flags} |")
    if filtered_out:
        lines.append("\n## 未通过交通硬约束（保留备查）\n")
        lines.append("| 名称 | 交通距离(m) | 综合分 |")
        lines.append("|---|---|---|")
        for r in filtered_out:
            td = r["transit_dist_m"] if r["transit_dist_m"] is not None else "—"
            lines.append(f"| {r['name']} | {td} | {r['score']} |")
    lines.append("\n## 已知缺陷\n")
    lines.append("1. 交通距离是直线距离，非步行路网距离；需真实可达性请接路径规划 API。")
    lines.append("2. OSM 中国场地 POI 覆盖有限，团建类场地（拓展基地/轰趴馆/农庄）常搜不到。")
    lines.append("3. 容量字段在 OSM 极少标注，多数候选项为「未知」并按中性分处理。\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_geojson(path, origin, radius):
    # 用 64 边形近似圆
    ring = []
    for i in range(65):
        ang = 2 * math.pi * i / 64
        dlat = (radius / EARTH_R) * (180 / math.pi) * math.cos(ang)
        dlon = (radius / EARTH_R) * (180 / math.pi) * math.sin(ang) / max(
            math.cos(math.radians(origin["lat"])), 1e-6)
        ring.append([round(origin["lon"] + dlon, 6), round(origin["lat"] + dlat, 6)])
    gj = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": f"range-{radius}m",
                           "origin": origin["name"],
                           "radius_m": radius},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }, {
            "type": "Feature",
            "properties": {"name": origin["name"], "role": "origin"},
            "geometry": {"type": "Point",
                         "coordinates": [origin["lon"], origin["lat"]]},
        }],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(gj, f, ensure_ascii=False, indent=2)


def write_map(path, origin, radius, rows):
    """Leaflet 可视化：范围圆 + 原点 + 候选点（按分数着色）"""
    def color(s):
        if s >= 75:
            return "#16a34a"
        if s >= 60:
            return "#f59e0b"
        return "#ef4444"

    items = []
    for r in rows:
        area = r["area_m2"] if r["area_m2"] else "未知"
        cap = r["capacity"] if r["capacity"] else "未知"
        td = r["transit_dist_m"] if r["transit_dist_m"] is not None else "未知"
        popup = (f"<b>{r['name']}</b><br>综合分 {r['score']}｜{r['kind']}<br>"
                 f"距原点 {r['dist_to_origin_m']} m<br>"
                 f"最近交通 {r['nearest_transit']}（{td} m）<br>"
                 f"面积 {area} m²｜容量 {cap}<br>"
                 f"<i>{'；'.join(r['flags'])}</i>")
        items.append({
            "lat": r["lat"], "lon": r["lon"], "name": r["name"],
            "color": color(r["score"]), "popup": popup,
            "rank": r["rank"],
        })

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>radius-dispatch 选址范围图</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{height:100%;margin:0;font-family:sans-serif}</style>
</head><body><div id="map"></div><script>
var map=L.map('map').setView([__LAT__,__LON__],__ZOOM__);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,
 attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
L.circle([__LAT__,__LON__],{radius:__RADIUS__,color:'#2563eb',weight:2,
 fillColor:'#3b82f6',fillOpacity:0.10}).addTo(map);
L.marker([__LAT__,__LON__]).addTo(map)
 .bindPopup('<b>__ORIGIN__</b><br>范围原点，半径 __RADIUS__ m').openPopup();
var items=__ITEMS__;
items.forEach(function(it){
  L.circleMarker([it.lat,it.lon],{radius:7,color:'#fff',weight:1.5,
    fillColor:it.color,fillOpacity:0.9}).addTo(map)
    .bindPopup(it.popup).bindTooltip(it.rank+'. '+it.name,{permanent:false});
});
</script></body></html>"""
    zoom = 14 if radius <= 1000 else (13 if radius <= 3000 else 12)
    html = (html.replace("__LAT__", f"{origin['lat']:.6f}")
                .replace("__LON__", f"{origin['lon']:.6f}")
                .replace("__RADIUS__", str(int(radius)))
                .replace("__ZOOM__", str(zoom))
                .replace("__ORIGIN__", origin["name"].replace("'", "\\'"))
                .replace("__ITEMS__", json.dumps(items, ensure_ascii=False)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ----------------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------------
def cmd_types(_):
    print("内置业务类型：\n")
    for k, v in BUSINESS_TYPES.items():
        print(f"  {k}")
        print(f"    名称：{v['label']}")
        print(f"    说明：{v['summary']}")
        print(f"    权重：交通 {v['weights']['transit']} / 面积 {v['weights']['size']}"
              f" / 容量 {v['weights']['capacity']}")
        print(f"    默认：最大交通距离 {v['defaults']['max_transit_distance']}m，"
              f"目标容量 {v['defaults']['target_capacity']}，"
              f"面积满分线 {v['defaults']['full_score_area']}m²")
        print()


def cmd_geocode(args):
    hits = geocode(args.query, args.city, args.country)
    if not hits:
        print(f"没解析到：{args.query}。换更完整的名字，或直接用 --lat/--lon 给坐标。")
        return 1
    cleaned = clean_place_name(args.query)
    tip = f"（已自动清洗为「{cleaned}」）" if cleaned != args.query.strip() else ""
    print(f"“{args.query}” 解析结果{tip}，按匹配度排序：\n")
    for i, h in enumerate(hits, 1):
        mark = "   ← 推荐作原点" if i == 1 else ""
        print(f"  {i}. {h['name']}{mark}")
        print(f"     坐标 {h['lat']:.6f}, {h['lon']:.6f}｜类型 {h['type']}"
              f"｜查询式「{h['query_used']}」")
    return 0


def _resolve_provider(args):
    """确定数据源，返回 (provider_name, amap_instance_or_None)。
    规则：--provider 显式指定则服从；auto 时若有 key 用高德，否则 OSM。"""
    key = args.key or os.environ.get("AMAP_KEY", "")
    provider = args.provider
    if provider == "osm":
        return "osm", None
    if provider == "amap":
        if not key:
            print("错误：--provider amap 需要高德 key（--key 或环境变量 AMAP_KEY）。")
            return None, None
        if AmapProvider is None:
            print("错误：providers.py 未找到，无法使用高德数据源。")
            return None, None
        return "amap", AmapProvider(key)
    # auto
    if key:
        if AmapProvider is None:
            print("提示：检测到 key 但 providers.py 缺失，回退 OSM。")
            return "osm", None
        return "amap", AmapProvider(key)
    return "osm", None


def cmd_scout(args):
    cfg = BUSINESS_TYPES.get(args.type)
    if not cfg:
        print(f"未知业务类型：{args.type}。用 `types` 看支持的列表。")
        return 1
    d = cfg["defaults"]

    provider, amap = _resolve_provider(args)
    if provider is None:
        return 1
    print(f"[数据源] {'高德(AMAP)' if provider == 'amap' else 'OpenStreetMap(免 key)'}")

    # 1) 原点
    if args.lat is not None and args.lon is not None:
        origin = {"name": f"{args.lat},{args.lon}", "lat": args.lat, "lon": args.lon}
    else:
        if not args.origin:
            print("需要 --origin 或 --lat/--lon")
            return 1
        if provider == "amap":
            try:
                hits = amap.geocode(args.origin, args.city)
            except Exception as e:
                print(f"高德地理编码失败：{e}")
                return 1
            if not hits:
                print(f"高德没解析到：{args.origin}。换更完整的名字，或直接用 --lat/--lon。")
                return 1
            origin = {"name": hits[0]["name"], "lat": hits[0]["lat"], "lon": hits[0]["lon"]}
            print(f"      （高德地理编码，级别 {hits[0]['type'].replace('高德:','') or '未知'}）")
        else:
            hits = geocode(args.origin, args.city, args.country)
            if not hits:
                print(f"原点解析失败：{args.origin}。换更完整的名字，或直接用 --lat/--lon。")
                return 1
            origin = {"name": hits[0]["name"], "lat": hits[0]["lat"], "lon": hits[0]["lon"]}
            print(f"      （查询式「{hits[0]['query_used']}」，类型 {hits[0]['type']}）")
    print(f"[1/6] 原点：{origin['name']}（{origin['lat']:.6f}, {origin['lon']:.6f}）")

    radius = args.radius
    max_transit = args.max_transit if args.max_transit is not None else d["max_transit_distance"]
    target_capacity = args.target_capacity if args.target_capacity is not None else d["target_capacity"]
    full_score_area = d["full_score_area"]

    # 2) 画范围
    os.makedirs(args.out_dir, exist_ok=True)
    write_geojson(os.path.join(args.out_dir, "range.geojson"), origin, radius)
    print(f"[2/6] 范围：半径 {radius} m 已生成 → range.geojson")

    # 3) 捞 POI
    if provider == "amap":
        kws = cfg.get("amap_keywords", []) or cfg.get("name_keywords", [])
        print(f"[3/6] 正在高德周边搜索候选场地（关键词 {len(kws)} 个）…")
        try:
            cands = amap.search_in_radius(origin["lat"], origin["lon"], radius, kws)
        except Exception as e:
            print(f"\n高德周边搜索失败：{e}")
            return 1
        print(f"      检索到 {len(cands)} 个候选")
    else:
        q = build_candidate_query(cfg, origin["lat"], origin["lon"], radius)
        print("[3/6] 正在 Overpass 检索候选场地（可能十几秒，公共节点偶发超时会自动重试）…")
        try:
            data = overpass(q)
        except Exception as e:
            print(f"\n候选检索失败：{e}")
            print("建议：缩小 --radius（如 1000）、稍后重试，或改用 --lat/--lon 分段跑。")
            return 1
        cands = parse_elements(data.get("elements", []))
        print(f"      检索到 {len(cands)} 个候选")

    # 4) 交通点
    if provider == "amap":
        print("[4/6] 正在高德检索范围内交通站点…")
        # 交通站点不全 -> 「到最近站点距离」偏大 -> 硬约束误杀 -> 排名失真。
        # 故单独统计它的失败次数，出问题要显式报警，不能静默带残缺数据往下跑。
        amap.failed_queries.clear()
        try:
            transits_raw = amap.search_transit(origin["lat"], origin["lon"], radius + 800)
        except Exception as e:
            print(f"\n高德交通站点检索失败：{e}")
            return 1
        n_fail = len(amap.failed_queries)
        # 高德返回的是候选结构，转成 enrich 期望的 {name,lat,lon,type}
        transits = [{"name": c["name"], "lat": c["lat"], "lon": c["lon"],
                     "type": c.get("kind", "")} for c in transits_raw]
        print(f"      找到 {len(transits)} 个交通站点")
        if n_fail:
            print(f"      ⚠ {n_fail} 个关键词检索失败（限流/错误），交通站点可能不全，"
                  f"「到站点距离」会偏大")
        if not transits:
            print("      ⚠ 未找到任何交通站点，交通维度将全部为「未知」，排名不可信")
    else:
        print("[4/6] 正在检索范围内交通站点…")
        try:
            tdata = overpass(build_transit_query(origin["lat"], origin["lon"], radius + 800))
        except Exception as e:
            print(f"\n交通站点检索失败：{e}")
            print("建议：稍后重试，或缩小 --radius 后重跑。")
            return 1
        transits = parse_transit(tdata.get("elements", []))
        print(f"      找到 {len(transits)} 个交通站点")
    cands = enrich(cands, transits, origin)

    # 5) 打分排序
    cands = score_candidates(cands, cfg, max_transit, target_capacity, full_score_area)
    # 同分时：交通距离近的优先，其次面积大的优先
    cands.sort(key=lambda c: (-c["score"],
                              c["transit_dist_m"] if c["transit_dist_m"] is not None else 9e9,
                              -c["area_m2"]))
    passed, failed = [], []
    for c in cands:
        if args.keep_all:
            passed.append(c)
        elif c["transit_dist_m"] is None or c["transit_dist_m"] > max_transit:
            failed.append(c)
        else:
            passed.append(c)
    for i, c in enumerate(passed, 1):
        c["rank"] = i

    # 6) 出产物
    rows = passed[:args.top]
    out_csv = os.path.join(args.out_dir, "candidates.csv")
    out_json = os.path.join(args.out_dir, "candidates.json")
    out_md = os.path.join(args.out_dir, "report.md")
    write_csv(out_csv, rows)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"meta": {"origin": origin, "radius_m": radius, "type": args.type,
                            "max_transit_m": max_transit,
                            "target_capacity": target_capacity,
                            "total": len(cands), "passed": len(passed)},
                   "candidates": passed}, f, ensure_ascii=False, indent=2)
    meta = {"origin_name": origin["name"], "lat": origin["lat"], "lon": origin["lon"],
            "radius": radius, "type_label": cfg["label"],
            "type_summary": cfg["summary"], "max_transit": max_transit,
            "target_capacity": target_capacity, "total": len(cands),
            "passed": len(passed)}
    write_report(out_md, meta, rows, failed)
    if not args.no_map:
        write_map(os.path.join(args.out_dir, "map.html"), origin, radius, rows)
    print(f"[6/6] 产物已写入 {os.path.abspath(args.out_dir)}")

    # 控制台摘要
    print(f"\n通过交通硬约束 {len(passed)} / {len(cands)} 个，展示前 {len(rows)} 个：\n")
    print(f"{'#':>3}  {'名称':<26} {'分':>5}  {'距原点':>7}  {'交通':>7}  最近站点")
    print("-" * 92)
    for r in rows:
        td = r["transit_dist_m"] if r["transit_dist_m"] is not None else "—"
        nm = r["name"][:24]
        print(f"{r['rank']:>3}  {nm:<26} {r['score']:>5}  "
              f"{r['dist_to_origin_m']:>7}  {str(td):>7}  {r['nearest_transit'][:18]}")
    if failed:
        print(f"\n（另有 {len(failed)} 个超出交通硬约束 {max_transit}m，已记入 report.md 备查）")
    return 0


def main():
    p = argparse.ArgumentParser(
        prog="radius_dispatch.py",
        description="radius-dispatch · 地点范围测定 / 选址 scout（OpenStreetMap，免 key）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("types", help="列出内置业务类型").set_defaults(func=cmd_types)

    g = sub.add_parser("geocode", help="地名/地铁站名 → 坐标")
    g.add_argument("query")
    g.add_argument("--city", default=None, help="城市名，消歧用")
    g.add_argument("--country", default="cn",
                   help="国家码过滤，默认 cn（中国大陆）；传空串 '' 关闭过滤")
    g.set_defaults(func=cmd_geocode)

    s = sub.add_parser("scout", help="完整流水线：画范围 → 捞 POI → 过滤打分 → 清单")
    s.add_argument("--origin", default=None, help="原点名称（地铁站/地名/地址）")
    s.add_argument("--city", default=None, help="城市名，消歧用")
    s.add_argument("--country", default="cn",
                   help="地理编码国家过滤，默认 cn；传空串 '' 关闭过滤")
    s.add_argument("--lat", type=float, default=None)
    s.add_argument("--lon", type=float, default=None)
    s.add_argument("--radius", type=int, default=1500, help="范围半径（米），默认 1500")
    s.add_argument("--type", default="team-building",
                   choices=list(BUSINESS_TYPES.keys()), help="业务类型")
    s.add_argument("--max-transit", type=int, default=None, help="交通硬约束最大距离（米）")
    s.add_argument("--target-capacity", type=int, default=None, help="目标容量（人）")
    s.add_argument("--top", type=int, default=20, help="清单展示条数")
    s.add_argument("--out-dir", default="./radius-dispatch-out", help="产物输出目录")
    s.add_argument("--keep-all", action="store_true", help="不过滤交通不达标项")
    s.add_argument("--no-map", action="store_true", help="不生成 HTML 地图")
    s.add_argument("--provider", default="auto", choices=["auto", "osm", "amap"],
                   help="数据源：auto(有 key 用高德，否则 OSM) / osm / amap。默认 auto")
    s.add_argument("--key", default=None,
                   help="高德 Web 服务 key；不传则读环境变量 AMAP_KEY")
    s.set_defaults(func=cmd_scout)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
