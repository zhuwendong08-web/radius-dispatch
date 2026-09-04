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
    from providers import AmapProvider, wgs84_to_gcj02
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
            # 小容量业态：即便名字标了「团建/轰趴」，这类体验馆也装不下 200~300 人
            # （手作/电玩/剧本杀等是 10~30 人小团业态）。优先级必须在 strong 之前，
            # 否则「XX手作·团建」「XX电玩·轰趴」会命中团建/轰趴拿满分。
            # 市中心实测暴露：虎门没有这类场地，市中心(手作集合馆/电玩轰趴馆)就现形了。
            "small_venue": ["手作", "diy", "DIY", "奶油胶", "石膏", "娃娃", "陶艺",
                            "绘画", "画画", "美术", "黏土", "流体熊", "tufting",
                            "电玩", "游戏厅", "游戏馆", "ps5", "PS5", "switch",
                            "剧本杀", "桌游", "麻将", "棋牌", "密室", "猫咖",
                            "狗咖", "娃娃机", "抓娃娃"],
            "small_venue_score": 55,
            # 强意图：名字里直接写了就是干这个的。
            # 注意「烧烤/采摘/钓虾」等不能单独算强信号——它们是营地的「项目」，
            # 不是场地本身。「XX烧烤王」「XX羊肉烧烤」是烧烤店，不是团建场地。
            # 它们与营地类词（营地/团建/农庄等）同现时，营地词会先命中返回高分；
            # 单独出现时落到 weak 层低分。实测把「烧烤」放 strong 会让纯烧烤店混进前排。
            "strong": {"团建": 95, "拓展": 95, "拓展基地": 98, "轰趴": 90,
                       "真人CS": 92, "CS": 90, "营地": 88, "露营": 85,
                       "户外": 78},
            # 中等意图：场地类型对（有大片空地，能装下 200 人），但没明说用途
            "medium": {"生态园": 88, "农庄": 85, "山庄": 85, "农家乐": 85,
                       "庄园": 82, "农场": 82, "度假": 80, "度假村": 82,
                       "乐园": 72, "会议中心": 78, "宴会厅": 70, "活动中心": 70,
                       "烧烤乐园": 80, "采摘园": 78, "垂钓园": 70, "钓虾": 68},
            # 负向：是运动/休闲场地，但装不下 200~300 人（容量不符，非用途不符）
            #   实测这批是最大的"高分噪声"——篮球场/游泳馆/健身中心离地铁近，
            #   交通分顶格，若无此层会霸占前排，把真正的农庄挤下去。
            "weak": {"球场": 30, "篮球": 25, "网球": 25, "足球": 30,
                     "羽毛球": 30, "乒乓球": 25, "游泳": 30, "健身": 25,
                     "台球": 25, "瑜伽": 25, "舞蹈": 30, "跆拳道": 25,
                     "武馆": 30, "溜冰": 25, "烧烤": 30, "采摘": 32,
                     "钓虾": 30, "垂钓": 30},
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
    """补：到原点距离、各候选直线前 3 的交通站点（供步行算路择优）。
    存 nearest_stations 候选表；nearest_transit/transit_dist_m 初始为直线最近站。"""
    for c in cands:
        c["dist_to_origin_m"] = round(haversine(origin["lat"], origin["lon"],
                                                c["lat"], c["lon"]), 1)
        ds = []
        for t in transits:
            d = haversine(c["lat"], c["lon"], t["lat"], t["lon"])
            ds.append((d, t["name"], t["type"], t["lat"], t["lon"]))
        if ds:
            ds.sort(key=lambda x: x[0])
            top = ds[:3]                       # 直线前 3，给步行择优用
            best = top[0]
            c["nearest_stations"] = [
                {"name": x[1], "type": x[2], "lat": x[3], "lon": x[4],
                 "line_dist_m": round(x[0], 1)} for x in top]
            c["nearest_transit"] = best[1]
            c["transit_dist_m"] = round(best[0], 1)   # 直线距离（初始）
            c["nearest_transit_type"] = best[2]
            c["nearest_lat"] = best[3]
            c["nearest_lon"] = best[4]
            c["transit_mode"] = "line"                # line=直线 / walk=步行路网
        else:
            c["nearest_stations"] = []
            c["nearest_transit"] = ""
            c["transit_dist_m"] = None
            c["nearest_transit_type"] = ""
            c["transit_mode"] = "line"
    return cands


def compute_walk_distances(cands, amap, max_transit=1000):
    """
    把「到站点」距离从直线升级为真实步行路网距离（仅高德数据源）。
    **对每个候选的 top-3 站点各算一次步行，取最小**——直线最近 ≠ 步行最近
    （实测「田园乐露营」直线 28m 到站、实走 189m；最近站点若在马路对面/封闭侧，
    第二个站点反而更近）。
    明显超约束的站点跳过（步行只会更远，算它无意义），省配额。
    失败保留直线并标记。返回 (成功数, 失败数)。
    """
    n_ok = n_fail = 0
    for c in cands:
        stations = [s for s in c.get("nearest_stations") or []
                    if s["line_dist_m"] <= max_transit * 1.5]
        if not stations:
            c["walk_fail"] = True
            n_fail += 1
            continue
        best_walk = None                       # (dist, station)
        for st in stations:
            d = _route_retry(amap, (c["lat"], c["lon"]), (st["lat"], st["lon"]))
            if d is not None and d > 0 and (best_walk is None or d < best_walk[0]):
                best_walk = (d, st)
        if best_walk:
            c["line_dist_m"] = c["transit_dist_m"]   # 保留直线最近站距离，供对照
            c["transit_dist_m"] = round(best_walk[0], 1)
            c["nearest_transit"] = best_walk[1]["name"]
            c["nearest_lat"] = best_walk[1]["lat"]
            c["nearest_lon"] = best_walk[1]["lon"]
            c["transit_mode"] = "walk"
            n_ok += 1
        else:
            c["walk_fail"] = True                    # score 阶段转成 flags
            n_fail += 1
    return n_ok, n_fail


def _route_retry(amap, a, b, _try=0):
    """两点步行算路，失败延迟重试一次（实测失败多为 QPS 限流，重试能救回大半）。
    a, b 均为 (lat, lon) 内部 WGS84。"""
    try:
        return amap.route_distance(a, b)
    except Exception:
        if _try < 1:
            time.sleep(1.5)                          # 避让限流窗口
            return _route_retry(amap, a, b, _try + 1)
        return None


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

    信号优先级：噪声词 > 小容量业态 > 名称强意图 > 名称中等意图 > 负向(容量不足) > 类目 > 默认。
    名字里直接写用途（如「小满营地烧烤.采摘.团建」）比平台归档的类目更可信。
    但要注意：手作/电玩/剧本杀这类体验馆即使名字标了「团建/轰趴」，也装不下 200~300 人，
    故「小容量业态」检查必须在 strong 之前，防止它们蹭团建满分。
    """
    if not cfg_suit:
        return 50.0, []
    text = f"{name or ''} {kind or ''}"
    # 1) 噪声词优先判定：命中噪声直接给低分，不再看其他信号
    #    （如「XX小吃实训机构」虽含「机构」，但明显不是团建场地）
    for w in cfg_suit.get("noise", []):
        if w in text:
            return float(cfg_suit.get("noise_score", 12)), [f"疑似非目标场地(含「{w}」)"]
    # 1.5) 小容量业态：手作/电玩/剧本杀等体验馆，标了团建/轰趴也装不下大团
    for w in cfg_suit.get("small_venue", []):
        if w in text:
            return float(cfg_suit.get("small_venue_score", 55)), \
                [f"疑似小容量业态(「{w}」类体验馆)，难装200+人"]
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
        # 步行算路失败的候选：距离按直线计，提示用户该距离偏乐观
        if c.get("walk_fail"):
            flags.append("步行算路失败，按直线距离计(偏乐观)")

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


def derive_checklist(flags):
    """从 flags 推导「打电话时要核实什么」。flags 的完整清单见 score_candidates。"""
    checks = []
    if any("容量" in f for f in flags):
        checks.append("核实能否容纳目标人数")
    if any("按类型推断" in f for f in flags):
        checks.append("核实实际接待能力")
    if any("算路失败" in f for f in flags):
        checks.append("核实到站实际步行")
    if any("未知" in f for f in flags):
        checks.append("确认营业状态与规模")
    if not checks:
        checks.append("报团建价/查档期")
    return "；".join(checks)


CONTACT_FIELDS = ["rank", "name", "score", "dist_to_origin_m", "transit_dist_m",
                  "nearest_transit", "phone", "address", "kind", "checklist"]


def write_contacts(path, rows, origin_name, target_capacity):
    """联系核实版 CSV：带电话/地址/需人工核实事项，供直接打电话用。
    地址全有、电话约 55% 覆盖（高德 POI 有 tel 的才有），无电话的需地图 App 搜名字。"""
    out = []
    for r in rows:
        out.append({
            "rank": r["rank"], "name": r["name"], "score": r["score"],
            "dist_to_origin_m": r["dist_to_origin_m"],
            "transit_dist_m": r["transit_dist_m"],
            "nearest_transit": r["nearest_transit"],
            "phone": r.get("phone") or "",
            "address": r.get("address") or "",
            "kind": r.get("kind") or "",
            "checklist": derive_checklist(r.get("flags") or []),
        })
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CONTACT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)
    return out


def write_report(path, meta, rows, filtered_out):
    lines = []
    lines.append("# 选址候选清单 · radius-dispatch\n")
    lines.append("## 范围设定\n")
    lines.append(f"- 原点：**{meta['origin_name']}**（{meta['lat']:.6f}, {meta['lon']:.6f}）")
    note = meta.get("range_note") or f"半径 {meta['radius']} m 圆"
    lines.append(f"- 范围：**{note}**（已输出 GeoJSON / 地图）")
    lines.append(f"- 业务类型：**{meta['type_label']}** — {meta['type_summary']}")
    lines.append(f"- 交通硬约束：候选点到最近交通站点 ≤ **{meta['max_transit']} m**"
                 f"（{meta.get('transit_note', '直线距离')}）")
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
    if meta.get("transit_note", "") == "真实步行路网距离":
        lines.append("1. 交通距离为**真实步行路网距离**（高德步行路径规划）；算路失败的候选回退直线并已标注。")
    else:
        lines.append("1. 交通距离是直线距离，非步行路网距离；高德数据源加 `--provider amap`（去掉 `--no-walk`）即为真实步行距离。")
    lines.append("2. OSM 中国场地 POI 覆盖有限，团建类场地（拓展基地/轰趴馆/农庄）常搜不到，建议高德数据源。")
    lines.append("3. 面积/容量两数据源均极少标注（高德不返回），多数候选项为「未知」并按类型推断/中性分处理，须人工核实。\n")
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


def write_map(path, origin, radius, rows, iso_pts=None, minutes=None):
    """Leaflet 可视化：范围(圆或等时圈多边形) + 原点 + 候选点（按分数着色）"""
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

    if iso_pts:
        # 等时圈多边形渲染
        ring = [[round(x[1], 6), round(x[0], 6)] for x in iso_pts]
        html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>radius-dispatch 等时圈选址</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{height:100%;margin:0;font-family:sans-serif}</style>
</head><body><div id="map"></div><script>
var map=L.map('map').setView([__LAT__,__LON__],__ZOOM__);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,
 attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
var ring=__RING__;
L.polygon(ring,{color:'#dc2626',weight:2,fillColor:'#ef4444',fillOpacity:0.08}).addTo(map);
L.marker([__LAT__,__LON__]).addTo(map)
 .bindPopup('<b>__ORIGIN__</b><br>驾车 __MINUTES__ 分钟可达范围').openPopup();
var items=__ITEMS__;
items.forEach(function(it){
  L.circleMarker([it.lat,it.lon],{radius:7,color:'#fff',weight:1.5,
    fillColor:it.color,fillOpacity:0.9}).addTo(map)
    .bindPopup(it.popup).bindTooltip(it.rank+'. '+it.name,{permanent:false});
});
</script></body></html>"""
        # 用候选点算视野（让范围自适应）
        if items:
            lats = [origin["lat"]] + [i["lat"] for i in items]
            lons = [origin["lon"]] + [i["lon"] for i in items]
            zoom = 12 if (max(lons) - min(lons)) < 0.05 else 11
        else:
            zoom = 12
        html = (html.replace("__LAT__", f"{origin['lat']:.6f}")
                    .replace("__LON__", f"{origin['lon']:.6f}")
                    .replace("__ZOOM__", str(zoom))
                    .replace("__RING__", json.dumps(ring))
                    .replace("__ORIGIN__", origin["name"].replace("'", "\\'"))
                    .replace("__MINUTES__", str(minutes))
                    .replace("__ITEMS__", json.dumps(items, ensure_ascii=False)))
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return

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

    # 1.5) 等时圈模式：--isochrone-minutes 设置时，范围改用驾车分钟数。
    # 生成等时圈多边形，POI 检索半径取它的外接圆（高德单次周边搜索上限内），
    # 候选再按「是否落在多边形内」过滤 → 得到真实驾车可达范围内的场地。
    iso_pts = None
    if getattr(args, "isochrone_minutes", None):
        if provider != "amap":
            print("错误：--isochrone-minutes 依赖高德驾车规划，需 --provider amap 且提供 key。")
            return 1
        print(f"[1.5/6] 正在生成驾车 {args.isochrone_minutes} 分钟等时圈"
              f"（{args.iso_directions} 方向二分）…")
        iso_pts = []
        for i in range(args.iso_directions):
            bearing = 360.0 * i / args.iso_directions
            iso_pts.append(_isochrone_boundary(
                amap, origin["lat"], origin["lon"], bearing,
                args.isochrone_minutes, args.iso_max_dist, args.iso_iterations))
        dmax = max(haversine(origin["lat"], origin["lon"], p[0], p[1])
                   for p in iso_pts)
        radius = int(dmax * 1.2 + 1000)          # 外接圆做检索，多边形做过滤
        print(f"      等时圈最远 {dmax:.0f} m；检索用外接圆 {radius} m，"
              f"再用多边形精确过滤")

    # 2) 画范围
    os.makedirs(args.out_dir, exist_ok=True)
    if iso_pts:
        write_polygon_geojson(os.path.join(args.out_dir, "range.geojson"), origin,
                              iso_pts, f"isochrone-{args.isochrone_minutes}min",
                              minutes=args.isochrone_minutes)
        print(f"[2/6] 范围：驾车 {args.isochrone_minutes} 分钟等时圈已生成 → range.geojson")
    else:
        write_geojson(os.path.join(args.out_dir, "range.geojson"), origin, radius)
        print(f"[2/6] 范围：半径 {radius} m 已生成 → range.geojson")

    # 3) 捞 POI
    if provider == "amap":
        kws = cfg.get("amap_keywords", []) or cfg.get("name_keywords", [])
        print(f"[3/6] 正在高德周边搜索候选场地（关键词 {len(kws)} 个）…")
        try:
            cands = amap.search_in_radius(
                origin["lat"], origin["lon"], radius, kws,
                limit=500 if iso_pts else 200)   # 等时圈外接圆大，放宽截断
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

    # 3.5) 等时圈模式：候选必须落在等时圈多边形内（外接圆只是检索用的超集）
    if iso_pts:
        inside, outside = [], []
        for c in cands:
            (inside if point_in_polygon(c["lat"], c["lon"], iso_pts)
             else outside).append(c)
        print(f"      等时圈内候选 {len(inside)} 个，多边形外剔除 {len(outside)} 个")
        cands = inside
        if not cands:
            print("      ⚠ 等时圈内没有候选场地。可加大 --minutes 或换关键词。")
            return 1

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

    # 4.5) 高德：把「到站点」距离从直线升级为真实步行路网距离。
    # 只对直线 <= max_transit 的候选算（步行 >= 直线，直线已超的必被淘汰，省配额）。
    # 步行距离会重新判定硬约束：直线达标但步行绕路超标的，会被正确淘汰。
    if provider == "amap" and not args.no_walk:
        for_walk = [c for c in cands
                    if c["transit_dist_m"] is not None
                    and c["transit_dist_m"] <= max_transit]
        n_skip = len(cands) - len(for_walk)
        print(f"[4.5/6] 正在计算到最近站点的真实步行距离"
              f"（{len(for_walk)} 个；{n_skip} 个直线已超约束，跳过省配额）…")
        n_ok, n_fail = compute_walk_distances(for_walk, amap, max_transit)
        print(f"      步行算路成功 {n_ok} 个，失败回退直线 {n_fail} 个")
        if n_ok:
            print("      （硬约束与排名将按真实步行距离重新判定，而非直线）")
        if n_fail:
            print(f"      ⚠ {n_fail} 个步行算路失败（限流/无路网），这些按直线距离计")

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
            "passed": len(passed),
            "range_note": (f"驾车 {args.isochrone_minutes} 分钟等时圈"
                           if iso_pts else f"半径 {radius} m 圆"),
            "transit_note": ("真实步行路网距离" if (provider == "amap" and not args.no_walk)
                             else "直线距离")}
    write_report(out_md, meta, rows, failed)
    if not args.no_map:
        write_map(os.path.join(args.out_dir, "map.html"), origin, radius, rows,
                  iso_pts=iso_pts, minutes=getattr(args, "isochrone_minutes", None))
    if getattr(args, "contacts", False):
        contact_path = os.path.join(args.out_dir, "contacts.csv")
        contact_rows = write_contacts(contact_path, passed, origin["name"],
                                      target_capacity)
        n_tel = sum(1 for r in contact_rows if r["phone"])
        print(f"      联系清单：{contact_path}（{len(contact_rows)} 个，"
              f"{n_tel} 个有电话）")
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


# ----------------------------------------------------------------------------
# isochrone：按「N 分钟车程」画范围（等时圈）
# 高德没有公开免费的等时圈多边形 API（要商业授权），但个人 key 可调驾车路径规划，
# 故用「方向采样 + 二分搜索」DIY：从原点向 N 个方向各找「恰好 minutes 分钟可达的最远点」，
# 边界点连成多边形即近似等时圈。驾驶路线沿道路延伸，形状自然反映高速/高架走向。
# ----------------------------------------------------------------------------
def _point_at(lat, lon, bearing_deg, dist_m):
    """从 (lat,lon) 沿方位角(度，0=北，顺时针) 走 dist_m 的目标点（WGS84 球面近似）"""
    ang = math.radians(bearing_deg)
    d = dist_m / EARTH_R
    lat1, lon1 = math.radians(lat), math.radians(lon)
    lat2 = math.asin(math.sin(lat1) * math.cos(d)
                     + math.cos(lat1) * math.sin(d) * math.cos(ang))
    lon2 = lon1 + math.atan2(math.sin(ang) * math.sin(d) * math.cos(lat1),
                             math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


def _drive_minutes(amap, o_lat, o_lon, d_lat, d_lon):
    """两点驾车时间（分钟）。内部 WGS84，进出高德边界转 GCJ02。失败返回 None。"""
    og = wgs84_to_gcj02(o_lon, o_lat)
    dg = wgs84_to_gcj02(d_lon, d_lat)
    try:
        r = amap._get("/v3/direction/driving", {
            "origin": f"{og[0]:.6f},{og[1]:.6f}",
            "destination": f"{dg[0]:.6f},{dg[1]:.6f}"})
        paths = r.get("route", {}).get("paths", []) or []
        if not paths:
            return None
        return float(paths[0].get("duration", 0)) / 60.0
    except Exception:
        return None


def _isochrone_boundary(amap, lat, lon, bearing, minutes,
                        max_dist=60000, iters=10):
    """沿一个方向二分「恰好 minutes 分钟可达的最远距离」，返回边界点 (lat, lon)。
    先探测上界（不足则翻倍扩），再迭代收敛。"""
    lo, hi = 0.0, float(max_dist)
    for _ in range(3):                       # 上界探测：不够远就翻倍
        d = _drive_minutes(amap, lat, lon, *_point_at(lat, lon, bearing, hi))
        if d is None or d >= minutes:
            break
        hi *= 2
    for _ in range(iters):
        mid = (lo + hi) / 2
        d = _drive_minutes(amap, lat, lon, *_point_at(lat, lon, bearing, mid))
        if d is None:
            hi = mid                          # 算路失败按超时收缩，保守
        elif d <= minutes:
            lo = mid
        else:
            hi = mid
    return _point_at(lat, lon, bearing, lo)


def point_in_polygon(lat, lon, poly):
    """射线法判断点 (lat,lon) 是否在多边形内。poly: [(lat,lon),...]。
    几十 km 范围内用经纬度平面近似足够（等时圈边界点密度 16/圈）。"""
    inside = False
    n = len(poly)
    for i in range(n):
        lat1, lon1 = poly[i]
        lat2, lon2 = poly[(i + 1) % n]
        if (lon1 > lon) != (lon2 > lon):          # 射线穿越该边
            x_inter = lat1 + (lon - lon1) / (lon2 - lon1) * (lat2 - lat1)
            if lat < x_inter:
                inside = not inside
    return inside


def write_polygon_geojson(path, origin, pts, name, minutes=None):
    """把等时圈边界点写成 GeoJSON（WGS84，与地图一致）。pts: [(lat,lon),...]"""
    ring = [[round(x[1], 6), round(x[0], 6)] for x in pts]
    ring.append(ring[0])
    props = {"name": name, "origin": origin["name"]}
    if minutes is not None:
        props["minutes"] = minutes
    gj = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": props,
        "geometry": {"type": "Polygon", "coordinates": [ring]}}, {
        "type": "Feature", "properties": {"name": origin["name"], "role": "origin"},
        "geometry": {"type": "Point",
                     "coordinates": [origin["lon"], origin["lat"]]}}]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(gj, f, ensure_ascii=False, indent=2)


def write_isochrone_map(path, origin, minutes, pts):
    """Leaflet 地图：等时圈多边形 + 原点"""
    ring = [[round(x[1], 6), round(x[0], 6)] for x in pts]   # (lat,lon)->[lon,lat]
    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>radius-dispatch 等时圈</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{height:100%;margin:0;font-family:sans-serif}</style>
</head><body><div id="map"></div><script>
var map=L.map('map').setView([__LAT__,__LON__],12);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,
 attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
var ring=__RING__;
L.polygon(ring,{color:'#dc2626',weight:2,fillColor:'#ef4444',fillOpacity:0.18}).addTo(map);
L.marker([__LAT__,__LON__]).addTo(map)
 .bindPopup('<b>__ORIGIN__</b><br>__MINUTES__ 分钟车程范围').openPopup();
map.fitBounds(L.latLngBounds(ring));
</script></body></html>"""
    html = (html.replace("__LAT__", f"{origin['lat']:.6f}")
                .replace("__LON__", f"{origin['lon']:.6f}")
                .replace("__RING__", json.dumps(ring))
                .replace("__ORIGIN__", origin["name"].replace("'", "\\'"))
                .replace("__MINUTES__", str(minutes)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def cmd_isochrone(args):
    """按驾车分钟数画范围：方向采样 + 二分，输出等时圈 GeoJSON + 地图"""
    provider, amap = _resolve_provider(args)
    if provider is None:
        return 1
    if amap is None:
        print("错误：等时圈依赖高德驾车路径规划，需要 --key 或环境变量 AMAP_KEY。")
        return 1
    print(f"[数据源] 高德(AMAP) 驾车路径规划")

    # 1) 原点
    if args.lat is not None and args.lon is not None:
        origin = {"name": f"{args.lat},{args.lon}", "lat": args.lat, "lon": args.lon}
    else:
        if not args.origin:
            print("需要 --origin 或 --lat/--lon")
            return 1
        hits = amap.geocode(args.origin, args.city)
        if not hits:
            print(f"原点解析失败：{args.origin}")
            return 1
        origin = {"name": hits[0]["name"], "lat": hits[0]["lat"], "lon": hits[0]["lon"]}
    print(f"[1/3] 原点：{origin['name']}（{origin['lat']:.6f}, {origin['lon']:.6f}）")
    print(f"      目标：驾车 {args.minutes} 分钟范围（{args.directions} 方向二分）")

    # 2) 各方向求边界点
    pts = []
    for i in range(args.directions):
        bearing = 360.0 * i / args.directions
        p = _isochrone_boundary(amap, origin["lat"], origin["lon"], bearing,
                                args.minutes, args.max_dist, args.iterations)
        dist = haversine(origin["lat"], origin["lon"], p[0], p[1])
        pts.append(p)
        if args.verbose or i == 0 or i == args.directions - 1:
            print(f"      方向 {bearing:>5.1f}°  可达 {dist:>6.0f} m")
        time.sleep(0.05)   # 不额外 sleep，_get 自带全局节流

    # 3) 出产物
    os.makedirs(args.out_dir, exist_ok=True)
    ring = [[round(x[1], 6), round(x[0], 6)] for x in pts]
    ring.append(ring[0])
    gj = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"name": f"isochrone-{args.minutes}min",
                       "origin": origin["name"], "minutes": args.minutes,
                       "mode": "driving"},
        "geometry": {"type": "Polygon", "coordinates": [ring]}}, {
        "type": "Feature", "properties": {"name": origin["name"], "role": "origin"},
        "geometry": {"type": "Point",
                     "coordinates": [origin["lon"], origin["lat"]]}}]}
    geo_path = os.path.join(args.out_dir, "isochrone.geojson")
    with open(geo_path, "w", encoding="utf-8") as f:
        json.dump(gj, f, ensure_ascii=False, indent=2)
    map_path = os.path.join(args.out_dir, "isochrone-map.html")
    write_isochrone_map(map_path, origin, args.minutes, pts)
    print(f"\n[3/3] 等时圈已生成：\n  {geo_path}\n  {map_path}")
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

    i = sub.add_parser("isochrone", help="按驾车分钟数画范围（等时圈，需高德 key）")
    i.add_argument("--origin", default=None, help="原点名称（地铁站/地名/地址）")
    i.add_argument("--city", default=None, help="城市名，消歧用")
    i.add_argument("--lat", type=float, default=None)
    i.add_argument("--lon", type=float, default=None)
    i.add_argument("--minutes", type=int, default=20, help="驾车分钟数，默认 20")
    i.add_argument("--directions", type=int, default=16,
                   help="方向采样数，默认 16（越多越圆滑，请求量随之翻倍）")
    i.add_argument("--max-dist", type=int, default=60000,
                   help="单方向二分上界（米），默认 60000；不足会自动翻倍")
    i.add_argument("--iterations", type=int, default=10,
                   help="每方向二分迭代次数，默认 10（精度 ~max_dist/2^iters）")
    i.add_argument("--out-dir", default="./radius-dispatch-out", help="产物输出目录")
    i.add_argument("--provider", default="auto", choices=["auto", "amap"],
                   help="等时圈依赖驾车规划，只支持高德；默认 auto")
    i.add_argument("--key", default=None,
                   help="高德 Web 服务 key；不传则读环境变量 AMAP_KEY")
    i.add_argument("--verbose", action="store_true", help="打印每方向可达距离")
    i.set_defaults(func=cmd_isochrone)

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
    s.add_argument("--no-walk", action="store_true",
                   help="高德数据源下跳过真实步行距离计算（用直线距离），用于对比")
    s.add_argument("--isochrone-minutes", type=int, default=None,
                   help="以驾车 N 分钟等时圈为范围（替代半径圆）筛场地，需高德 key")
    s.add_argument("--iso-directions", type=int, default=16,
                   help="等时圈方向采样数，默认 16")
    s.add_argument("--iso-max-dist", type=int, default=60000,
                   help="等时圈单方向二分上界（米），默认 60000")
    s.add_argument("--iso-iterations", type=int, default=10,
                   help="等时圈每方向二分迭代，默认 10")
    s.add_argument("--contacts", action="store_true",
                   help="额外输出 contacts.csv：带电话/地址/需人工核实事项的联系清单")
    s.add_argument("--provider", default="auto", choices=["auto", "osm", "amap"],
                   help="数据源：auto(有 key 用高德，否则 OSM) / osm / amap。默认 auto")
    s.add_argument("--key", default=None,
                   help="高德 Web 服务 key；不传则读环境变量 AMAP_KEY")
    s.set_defaults(func=cmd_scout)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
