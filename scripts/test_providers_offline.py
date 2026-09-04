#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
providers.py 离线回归测试（不联网、不消耗配额）
=====================================================
覆盖三件事：
  1. WGS84 <-> GCJ02 坐标转换的往返精度
  2. search_in_radius 的半径过滤 / 距离排序 / 截断保留最近
  3. 高德返回结构 -> 内部候选结构的字段映射

运行：python scripts/test_providers_offline.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from providers import (AmapProvider, haversine, wgs84_to_gcj02,
                       gcj02_to_wgs84)

# 东莞虎门站（真实高铁站），本次业务的原点
O_LAT, O_LON = 22.863235, 113.668290


class FakeAmap(AmapProvider):
    """只替换 _get，绕过网络，其余逻辑走真实代码路径"""

    def __init__(self, pois):
        self.key = "fake-for-test"
        self.pois = pois
        self.nreq = 0

    def _get(self, path, params):
        self.nreq += 1
        # 第一页给数据，第二页给空 -> 触发翻页终止
        return {"status": "1",
                "pois": self.pois if params.get("page") == "1" else []}


def _make_pois_gcj02(dists):
    """按给定距离造「高德返回」的 POI。
    注意：高德返回 GCJ02，故必须在 GCJ02 系里偏移，
    否则 _poi_to_candidate 转回 WGS84 后会凭空多出约 550~620m 误差。"""
    g_lng, g_lat = wgs84_to_gcj02(O_LON, O_LAT)
    out = []
    for i, d in enumerate(dists):
        out.append({
            "name": f"场地{d}m", "id": f"p{i}",
            "location": f"{g_lng:.6f},{g_lat + d / 111320.0:.6f}",
            "type": "体育休闲服务;运动场馆;综合体育馆",
            "typecode": "080100", "tel": "0769-12345678",
            "website": "http://example.com",
            "address": "东莞市虎门镇某某路1号",
        })
    return out


def test_coord_roundtrip():
    """往返转换误差应 < 1m；境外坐标应原样返回"""
    for label, (la, lo) in [("广州体育西路", (23.133818, 113.316135)),
                            ("东莞虎门站", (22.863235, 113.668290)),
                            ("北京国贸", (39.907, 116.4556))]:
        g_lng, g_lat = wgs84_to_gcj02(lo, la)
        w_lng, w_lat = gcj02_to_wgs84(g_lng, g_lat)
        off = haversine(la, lo, g_lat, g_lng)
        err = haversine(la, lo, w_lat, w_lng)
        assert err < 1.0, f"{label} 往返误差过大: {err:.3f}m"
        assert off > 300, f"{label} 偏移量异常小，转换可能没生效: {off:.1f}m"
        print(f"  {label}: 偏移 {off:6.1f} m | 往返误差 {err:.4f} m")

    # 境外（东京）应原样返回
    t_lng, t_lat = wgs84_to_gcj02(139.6917, 35.6895)
    assert abs(t_lng - 139.6917) < 1e-9 and abs(t_lat - 35.6895) < 1e-9
    print("  境外坐标(东京): 原样返回 ✓")


def test_radius_filter_and_sort():
    """乱序输入 -> 半径过滤 -> 按距离排序 -> 截断保留最近的"""
    # 含 1 个半径外(5000m)，故意乱序
    dists = [1200, 300, 5000, 150, 900, 250, 1100, 400, 100, 1400]
    fake = FakeAmap(_make_pois_gcj02(dists))

    res = fake.search_in_radius(O_LAT, O_LON, 1500, ["拓展基地"], limit=200)
    got = [c["dist_to_origin_m"] for c in res]
    assert len(res) == 9, f"半径过滤有误，期望 9 得到 {len(res)}"
    assert got == sorted(got), f"未按距离排序: {got}"
    assert all(d <= 1500 for d in got), "半径外未被过滤"
    print(f"  半径过滤+排序: {got}")

    # 距离还原精度（GCJ02->WGS84 往返后应接近原始值）
    for c in res:
        want = float(c["name"].replace("场地", "").replace("m", ""))
        assert abs(c["dist_to_origin_m"] - want) / want < 0.01, \
            f"{c['name']} 距离还原偏差过大: {c['dist_to_origin_m']}"
    print("  距离还原误差均 < 1% ✓")

    # 截断必须留下最近的
    res5 = fake.search_in_radius(O_LAT, O_LON, 1500, ["拓展基地"], limit=5)
    g5 = [c["dist_to_origin_m"] for c in res5]
    assert all(d <= 420 for d in g5), f"截断没留下最近的: {g5}"
    print(f"  limit=5 截断保留最近的: {g5} ✓")


def test_poi_field_mapping():
    """高德 POI -> 内部候选结构，字段映射正确且坐标已转 WGS84"""
    fake = FakeAmap(_make_pois_gcj02([300]))
    res = fake.search_in_radius(O_LAT, O_LON, 1500, ["拓展基地"])
    c = res[0]
    assert c["id"].startswith("amap/"), "id 前缀不对"
    assert c["kind"] == "综合体育馆", f"kind 应取 type 最后一段，实际 {c['kind']}"
    assert c["phone"] == "0769-12345678", "tel 未映射"
    assert c["website"] == "http://example.com", "website 未映射"
    assert c["address"], "address 未映射"
    # 面积/容量高德不返回，明确置空（下游会走中性分）
    assert c["area_m2"] == 0.0 and c["capacity"] is None
    # 坐标必须已转回 WGS84（与原点同坐标系才能算距离）
    d = haversine(O_LAT, O_LON, c["lat"], c["lon"])
    assert abs(d - 300) / 300 < 0.01, f"坐标未正确转回 WGS84: {d:.1f}m"
    print(f"  字段映射: kind={c['kind']} tel={c['phone']} "
          f"面积={c['area_m2']}(高德不返回) 容量={c['capacity']}(高德不返回) ✓")
    print(f"  坐标已转 WGS84，距原点 {d:.1f} m ✓")


def test_quota_guard():
    """翻页终止 与 max_requests 配额保护生效"""
    # 首页不足 25 条 -> 判定无更多数据，不再翻页（省配额）
    few = _make_pois_gcj02([100, 200, 300])
    fake = FakeAmap(few)
    fake.search_in_radius(O_LAT, O_LON, 1500, ["a", "b", "c", "d"])
    assert fake.nreq == 4, f"首页不足25条应停止翻页，实际 {fake.nreq} 次"
    print(f"  首页不足25条即停翻: 4 关键词共 {fake.nreq} 次请求 ✓")

    # 首页满 25 条 -> 会继续翻第 2 页（返回空后停）
    full = _make_pois_gcj02(list(range(100, 100 + 25 * 100, 100))[:25])
    fake2 = FakeAmap(full)
    fake2.search_in_radius(O_LAT, O_LON, 90000, ["a", "b"])  # 半径放宽避免被过滤
    assert fake2.nreq == 4, f"满页应翻第2页，2词×2页=4次，实际 {fake2.nreq}"
    print(f"  首页满25条继续翻页: 2 关键词共 {fake2.nreq} 次请求 ✓")

    # max_requests 硬上限
    fake3 = FakeAmap(full)
    fake3.search_in_radius(O_LAT, O_LON, 90000, ["a", "b", "c", "d"],
                           max_requests=3)
    assert fake3.nreq <= 3, f"max_requests 未生效: {fake3.nreq}"
    print(f"  max_requests=3 硬上限生效: 实际 {fake3.nreq} 次 ✓")


if __name__ == "__main__":
    print("=== 1) 坐标转换 ===")
    test_coord_roundtrip()
    print("\n=== 2) 半径过滤 / 排序 / 截断 ===")
    test_radius_filter_and_sort()
    print("\n=== 3) 字段映射 ===")
    test_poi_field_mapping()
    print("\n=== 4) 配额保护 ===")
    test_quota_guard()
    print("\n全部通过 ✓")
