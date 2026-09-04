#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
radius-dispatch · 高德开放平台 provider（中国落地数据源层）
=====================================================

内部统一用 WGS-84 坐标（与 OSM / Leaflet 底图一致），
所有进出高德 API 的坐标都在边界上做 WGS84 <-> GCJ02 转换。

不做这个转换会怎样：中国境内两者相差约 300~500 米——
足以把「距地铁 500m」算成「距地铁 800m」，直接改变硬约束判断结果。

需要的能力与对应高德 API：
    geocode            /v3/geocode/geo        地理编码
    search_in_radius   /v3/place/around       周边搜索（按半径，正是「画范围」所需）
    search_transit     /v3/place/around       交通站点检索
    route_distance     /v3/direction/walking  步行路径规划（真实路网距离，非直线）

Key 通过环境变量 AMAP_KEY 或 --amap-key 传入，脚本绝不落盘、不写进任何产物文件。
"""

import math
import time
import urllib.parse
import urllib.request

UA = "radius-dispatch/0.1 (local WorkBuddy skill; python-urllib)"
AMAP_BASE = "https://restapi.amap.com"
EARTH_R = 6371000.0

# 高德常见错误码 → 人话。10003（超配额）在个人账号上最容易撞到。
AMAP_ERR = {
    "10001": "key 不正确或已过期",
    "10002": "key 未开启对应服务（去控制台勾选 Web 服务）",
    "10003": "已超出日/月访问量配额",
    "10004": "单位时间内访问过于频繁（QPS 超限）",
    "10005": "IP 白名单限制（去控制台配置白名单）",
    "10009": "请求 key 与绑定平台不符（需 Web 服务类型）",
    "10012": "权限不足，该服务未开通",
    "10021": "参数缺失或非法",
    "30001": "请求过于频繁，请稍后重试",
    "30002": "账号余额不足",
}


# ----------------------------------------------------------------------------
# 坐标系转换：WGS84 <-> GCJ02
# ----------------------------------------------------------------------------
_PI = 3.1415926535897932384626
_A = 6378245.0              # 克拉索夫斯基椭球长半轴
_EE = 0.00669342162296594323


def _out_of_china(lng, lat):
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x, y):
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * _PI) + 40.0 * math.sin(y / 3.0 * _PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * _PI) + 320 * math.sin(y * _PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * _PI) + 40.0 * math.sin(x / 3.0 * _PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * _PI) + 300.0 * math.sin(x / 30.0 * _PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lng, lat):
    """WGS-84 → GCJ-02（火星坐标）。境外原样返回。"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * _PI)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * _PI)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng, lat):
    """
    GCJ-02 → WGS-84。境外原样返回。

    用迭代法：单步逆变换实测残差约 1.5 米，迭代 3 次可压到毫米级。
    （选址判断的粒度是百米级，1.5 米本也够用，但迭代成本几乎为零，没必要留着误差。）
    """
    if _out_of_china(lng, lat):
        return lng, lat
    w_lng, w_lat = lng, lat
    for _ in range(3):
        g_lng, g_lat = wgs84_to_gcj02(w_lng, w_lat)
        w_lng += lng - g_lng
        w_lat += lat - g_lat
    return w_lng, w_lat


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def http_get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        import json
        return json.loads(r.read().decode("utf-8"))


# ----------------------------------------------------------------------------
# 高德 provider
# ----------------------------------------------------------------------------
class AmapError(RuntimeError):
    """高德 API 业务错误。带 code，调用方才能按错误码区分处理
    （配额耗尽要上抛、QPS 限流要退避重试、其他错误才跳过）。"""

    def __init__(self, msg, code=""):
        super().__init__(msg)
        self.code = str(code)


# 需要退避重试的错误码：单位时间访问过频 / 请求过于频繁
QPS_CODES = ("10004", "30001")


class AmapProvider:
    name = "amap"

    # 全局最小请求间隔（秒）。所有走 _get 的高德请求统一节流，
    # 避免候选检索 + 交通检索 + 步行距离连发导致 QPS 超限。
    # 0.30s ≈ 3.3 QPS。实测 0.15s（6.7 QPS）在长流程（200+ 连续请求）下
    # 仍会触发限流导致步行算路大量失败，故取更保守的值。
    MIN_INTERVAL = 0.30
    _last_req = 0.0

    def __init__(self, key):
        if not key:
            raise ValueError("高德 key 为空")
        self.key = key
        self.failed_queries = []      # 失败的（关键词, 页码），供上层判断数据完整性

    def _throttle(self):
        """发请求前调用：距上次请求不足 MIN_INTERVAL 就 sleep 补齐。"""
        now = time.monotonic()
        wait = AmapProvider._last_req + AmapProvider.MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        AmapProvider._last_req = time.monotonic()

    def _get(self, path, params, _retry=0):
        p = dict(params)
        p["key"] = self.key
        p.setdefault("output", "JSON")
        self._throttle()
        try:
            data = http_get_json(f"{AMAP_BASE}{path}?{urllib.parse.urlencode(p)}")
        except Exception as e:
            # 网络层错误也退避重试（偶发断连）
            if _retry < 2:
                time.sleep(1.0 * (_retry + 1))
                return self._get(path, params, _retry + 1)
            raise AmapError(f"网络请求失败：{e}", code="NETWORK")
        status = str(data.get("status", ""))
        if status != "1":
            info = data.get("info") or data.get("infocode") or "未知错误"
            code = str(data.get("infocode") or "")
            # QPS 限流：退避重试，不能静默跳过
            # （实测：候选检索大量请求后会限流，导致后续交通检索残缺，
            #   交通数据不全 -> 距离失真 -> 排名出错，必须重试而不是放弃）
            if code in QPS_CODES and _retry < 3:
                time.sleep(1.5 * (_retry + 1))
                return self._get(path, params, _retry + 1)
            hint = AMAP_ERR.get(code, "")
            msg = f"高德 API 返回失败：{info}"
            if hint:
                msg += f"（{hint}）"
            raise AmapError(msg, code=code)
        return data

    # ---- 地理编码 ----
    def geocode(self, query, city=None, limit=5):
        params = {"address": query}
        if city:
            params["city"] = city
        data = self._get("/v3/geocode/geo", params)
        out = []
        for g in data.get("geocodes", [])[:limit]:
            loc = g.get("location", "")
            if not loc:
                continue
            try:
                lng, lat = [float(x) for x in loc.split(",")]
            except Exception:
                continue
            w_lng, w_lat = gcj02_to_wgs84(lng, lat)   # 统一转成内部 WGS84
            out.append({
                "name": g.get("formatted_address") or query,
                "lat": w_lat, "lon": w_lng,
                "type": "高德:" + (g.get("level") or ""),
                "importance": 1.0,
                "query_used": query,
            })
        return out

    # ---- 周边搜索（核心：按半径捞真实场地）----
    def search_in_radius(self, lat, lon, radius, keywords, typecodes=None,
                         limit=200, city=None, max_pages_per_kw=4,
                         max_requests=60):
        """
        keywords: 关键词列表（如 ['拓展基地','轰趴馆','农庄']），逐词检索后合并去重。
        返回结构与 OSM 版 parse_elements 一致，下游打分逻辑可复用。

        三个已处理的坑（均来自高德 v3 官方文档实测确认）：
        1. sortrule=distance 在「只传 keywords」时不生效
           —— 官方原文「sortrule参数设置距离排序在只传keywords参数的情况下不生效」。
           故不依赖 API 排序，改为自己按距离排序。
        2. 因此不能在关键词循环里按 limit 提前截断：结果本就乱序，
           截断会丢掉近的、留下远的。改为「全量取回 → 半径过滤 → 排序 → 截断」。
        3. city 与经纬度冲突时高德会直接返回空
           （官方：「若范围内有用户指定 city 的数据则返回，否则返回为空」）。
           故 city 默认不传，靠 location+radius 限定；调用方可按需显式传入。
        """
        g_lng, g_lat = wgs84_to_gcj02(lon, lat)      # 内部 WGS84 → 高德 GCJ02
        radius = min(int(radius), 50000)             # 高德单边上界 50000m
        seen, out, nreq = set(), [], 0
        for kw in keywords:
            for page in range(1, max_pages_per_kw + 1):
                if nreq >= max_requests:             # 配额保护，个人账号月配额有限
                    break
                params = {
                    "location": f"{g_lng:.6f},{g_lat:.6f}",
                    "radius": str(radius),
                    "keywords": kw,
                    "offset": "25",
                    "page": str(page),
                    "extensions": "all",
                }
                # sortrule 在只传 keywords 时不生效，不传也罢，避免误导
                if typecodes:
                    params["types"] = typecodes
                    params["sortrule"] = "distance"  # 有 types 时才真的生效
                if city:
                    params["city"] = city
                try:
                    data = self._get("/v3/place/around", params)
                    nreq += 1
                except AmapError as e:
                    if e.code == "10003":
                        raise                        # 配额耗尽：上抛，不静默吞掉
                    self.failed_queries.append((kw, page, e.code))
                    break                            # 其他错误跳过该词，继续下一个
                pois = data.get("pois", []) or []
                if not pois:
                    break
                for p in pois:
                    name = (p.get("name") or "").strip()
                    if not name:
                        continue
                    key = (name, p.get("id", ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    c = self._poi_to_candidate(p)
                    if c:
                        out.append(c)
                if len(pois) < 25:
                    break
                time.sleep(0.2)                       # 温和限流
            if nreq >= max_requests:
                break
        # 关键词搜索可能带回半径外的结果，用真实半径过滤
        within = [c for c in out
                  if haversine(lat, lon, c["lat"], c["lon"]) <= radius]
        # 自己按距离排序（API 不保证），保证 limit 截断留下的是最近的
        for c in within:
            c["dist_to_origin_m"] = round(haversine(lat, lon, c["lat"], c["lon"]), 1)
        within.sort(key=lambda c: c["dist_to_origin_m"])
        return within[:limit]

    # ---- 交通站点检索 ----
    def search_transit(self, lat, lon, radius):
        # 站点密度高，翻 3 页（75 条/词）足够覆盖，省配额给候选场地检索
        return self.search_in_radius(
            lat, lon, radius,
            keywords=["地铁站", "公交站", "火车站", "高铁站", "汽车站"],
            limit=200, max_pages_per_kw=3)

    def _poi_to_candidate(self, p):
        """高德 POI → 内部候选结构（字段与 OSM 版保持一致）"""
        loc = p.get("location", "")
        if not loc:
            return None
        try:
            lng, lat = [float(x) for x in loc.split(",")]
        except Exception:
            return None
        w_lng, w_lat = gcj02_to_wgs84(lng, lat)
        # 高德 type 形如「体育休闲服务;体育场馆;综合体育馆」，用最后一段作主类目
        raw_type = p.get("type") or ""
        parts = [x for x in raw_type.split(";") if x]
        kind = parts[-1] if parts else (p.get("typecode") or "")
        return {
            "id": f"amap/{p.get('id','')}",
            "name": p.get("name", ""),
            "lat": w_lat, "lon": w_lng,
            "kind": kind,
            "area_m2": 0.0,          # 高德不返回占地/建筑面积
            "capacity": None,        # 高德不返回可容纳人数
            "phone": p.get("tel") or "",
            "website": p.get("website") or "",
            "address": p.get("address") or "",
            "tags": {"amap_type": raw_type, "typecode": p.get("typecode", "")},
        }

    # ---- 真实步行距离（修复「直线距离」缺陷的关键）----
    def route_distance(self, a, b, mode="walking"):
        """
        a, b 均为 (lat, lon) 内部 WGS84。返回步行路网距离（米），失败返回 None。
        用高德步行路径规划，比直线距离贴近真实可达性。
        """
        g_lng1, g_lat1 = wgs84_to_gcj02(a[1], a[0])
        g_lng2, g_lat2 = wgs84_to_gcj02(b[1], b[0])
        path = "/v3/direction/walking"
        params = {"origin": f"{g_lng1:.6f},{g_lat1:.6f}",
                  "destination": f"{g_lng2:.6f},{g_lat2:.6f}"}
        try:
            data = self._get(path, params)
            paths = data.get("route", {}).get("paths", []) or []
            if not paths:
                return None
            return float(paths[0].get("distance"))
        except Exception:
            return None
