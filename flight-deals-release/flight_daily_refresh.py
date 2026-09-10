#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
特价航班每日刷新脚本
- 每天定时查询 TripNow 实时价格
- 原地更新 HTML 中的 dest-card 价格、角标、增删卡片
- 回旋镖航线总价重算
- 空区域隐藏，飞常准零直飞删除卡片
- validate_html.py 校验
- 输出当日价格变化简报

用法: python3 flight_daily_refresh.py result_mode [html_path] [work_dir]
result_mode: display_only / no_reply / auto
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from codeact_sdk import CodeActSDK

# ============== 常量配置 ==============
TRIPNOW_API_URL = "https://tripnowengine.133.cn/tripnow/v1/chat/completions"
TRIPNOW_API_KEY = "sk-live-H499GeM83gfgzg5ISQhGAIdIzJUMpf9WCP6z3BsW0Ik"
VARIFLIGHT_API_KEY = "sk-9Ejt3VrN1nci926R2tJQ_eqYRTGgGKJUtxEEZoASdbA"
VARIFLIGHT_BASE_URL = "https://ai.variflight.com/api/v1/mcp/data"

# 价格门槛（人民币）
PRICE_THRESHOLDS = {
    "domestic": 600,       # 国内直飞
    "intl": 1000,          # 国际/港澳台直飞
    "boom_domestic": 1000, # 国内回旋镖（兜底）
    "boom_intl": 2000,     # 国际回旋镖（兜底）
    "boom_domestic_2": 1000, # 国内两程回旋
    "boom_domestic_3": 1500, # 国内三程回旋
    "boom_intl_2": 2000,     # 国际两程回旋
    "boom_intl_3": 2500,     # 国际三程回旋
}

# 出发机场中文名
AP_NAMES = {
    "CZX": "常州奔牛",
    "WUX": "无锡硕放",
    "SHA": "上海虹桥",
    "PVG": "上海浦东",
}

# 城市中文名（用于查询提示词）
CITY_NAMES = {
    "PEK": "北京", "CAN": "广州", "CTU": "成都", "CKG": "重庆", "KMG": "昆明",
    "XIY": "西安", "HGH": "杭州", "WUH": "武汉", "DLC": "大连", "TAO": "青岛",
    "CSY": "长沙", "HAK": "海口", "TSN": "天津", "TNA": "济南", "CGO": "郑州",
    "XMN": "厦门", "FOC": "福州", "KWE": "贵阳", "LHW": "兰州", "SHE": "沈阳",
    "INC": "银川", "NKG": "南京", "SZX": "深圳", "SYX": "三亚", "KWL": "桂林",
    "LXA": "拉萨", "HRB": "哈尔滨", "HET": "呼和浩特",
    "ICN": "首尔", "BKK": "曼谷", "SGN": "胡志明市", "HKG": "香港", "MFM": "澳门",
    "SIN": "新加坡", "KUL": "吉隆坡", "TPE": "台北", "DPS": "巴厘岛",
    "HND": "东京", "KIX": "大阪",
}

# 区域标签（与 HTML data-region 对应）
REGION_LABELS = {
    "domestic": "国内",
    "intl_sea": "东南亚",
    "intl_je": "日韩",
    "intl_hkmt": "港澳台",
}

# 区域→目的地代码映射（用于分类）
REGION_DESTS = {
    "domestic": {"PEK", "CAN", "CTU", "CKG", "KMG", "XIY", "HGH", "WUH", "DLC",
                 "TAO", "CSY", "HAK", "TSN", "TNA", "CGO", "XMN", "FOC", "KWE",
                 "LHW", "SHE", "INC", "NKG", "SZX", "SYX", "KWL", "LXA", "HRB", "HET"},
    "intl_sea": {"BKK", "SIN", "KUL", "SGN", "DPS", "MNL"},
    "intl_je": {"ICN", "HND", "KIX"},
    "intl_hkmt": {"HKG", "MFM", "TPE"},
}


def dest_to_region(dest_code: str) -> str:
    """根据目的地代码返回 data-region 值"""
    if dest_code in REGION_DESTS["domestic"]:
        return "国内"
    if dest_code in REGION_DESTS["intl_sea"]:
        return "东南亚"
    if dest_code in REGION_DESTS["intl_je"]:
        return "日韩"
    if dest_code in REGION_DESTS["intl_hkmt"]:
        return "港澳台"
    return "其他"


def dest_category(dest_code: str) -> str:
    """返回 domestic / intl"""
    return "domestic" if dest_code in REGION_DESTS["domestic"] else "intl"


# ============== HTML 栈匹配工具 ==============
def find_div_blocks(html: str, class_prefix: str) -> list[tuple[int, int, str]]:
    """栈式匹配所有 class 以 class_prefix 开头的 <div> 块
    返回 [(start, end, text)]，end 是闭标签结束位置的下一位
    """
    results = []
    i = 0
    marker = f'<div class="{class_prefix}'
    while True:
        j = html.find(marker, i)
        if j == -1:
            break
        depth = 0
        k = j
        while k < len(html):
            if re.match(r"<div\b", html[k:], re.IGNORECASE):
                depth += 1
                k += 4
            elif re.match(r"</div>", html[k:], re.IGNORECASE):
                depth -= 1
                k += 6
                if depth == 0:
                    break
            else:
                k += 1
        results.append((j, k, html[j:k]))
        i = k
    return results


def find_outermost_div_by_id(html: str, div_id: str) -> tuple[int, int] | None:
    """根据 id 找最外层 div，返回 (start, end)"""
    marker = f'id="{div_id}"'
    j = html.find(marker)
    if j == -1:
        return None
    # 向前找到 <div 开始
    start = html.rfind("<div", 0, j)
    if start == -1:
        return None
    depth = 0
    k = start
    while k < len(html):
        if re.match(r"<div\b", html[k:], re.IGNORECASE):
            depth += 1
            k += 4
        elif re.match(r"</div>", html[k:], re.IGNORECASE):
            depth -= 1
            k += 6
            if depth == 0:
                break
        else:
            k += 1
    return (start, k)


# ============== TripNow API ==============
def tripnow_query(prompt: str, retries: int = 1) -> str | None:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TRIPNOW_API_KEY}",
    }
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                TRIPNOW_API_URL,
                headers=headers,
                json={"model": "tripnow", "messages": [{"role": "user", "content": prompt}]},
                timeout=30,
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
            elif r.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  429限流，等待{wait}秒", flush=True)
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code}: {r.text[:120]}", flush=True)
                time.sleep(2)
        except Exception as e:
            print(f"  请求异常: {e}", flush=True)
            time.sleep(2)
    return None


def parse_price(text: str) -> int | None:
    if not text:
        return None
    patterns = [
        r"最便宜[^。！!？?]{0,80}?[¥￥]\s*([\d,]{3,6})",
        r"仅[¥￥]\s*([\d,]{3,6})",
        r"[¥￥]\s*([\d,]{3,6})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            price = int(m.group(1).replace(",", ""))
            if 80 <= price <= 20000:
                return price
    return None


def parse_flight_info(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    fno = ""
    m = re.search(r"最便宜[^。]{0,60}?([A-Z0-9]{2}\d{3,4})", text)
    if m:
        fno = m.group(1)
    else:
        m2 = re.search(r"([A-Z0-9]{2}\d{3,4})", text)
        fno = m2.group(1) if m2 else ""
    dtime = ""
    m3 = re.search(r"(\d{1,2}:\d{2})起飞", text)
    if m3:
        dtime = m3.group(1)
    else:
        tm = re.findall(r"(\d{1,2}:\d{2})", text)
        dtime = tm[0] if tm else ""
    return fno, dtime


# ============== 飞常准数据 ==============
def load_variflight_routes(work_dir: str) -> dict[str, list]:
    """加载飞常准航线数据（优先主缓存，不存在则从 spotcheck 补）"""
    routes_path = os.path.join(work_dir, "variflight_v3_routes.json")
    spot_path = os.path.join(work_dir, "variflight_spotcheck_v3.json")

    routes = {}
    if os.path.exists(routes_path):
        with open(routes_path, encoding="utf-8") as f:
            routes = json.load(f)

    # 合并 spotcheck 数据（补充抽查航线）
    if os.path.exists(spot_path):
        with open(spot_path, encoding="utf-8") as f:
            spot = json.load(f)
        if isinstance(spot, dict):
            for k, v in spot.items():
                if v and isinstance(v, list) and len(v) > 0:
                    # 只有主缓存是空或没有时才用 spotcheck
                    if k not in routes or not routes[k]:
                        routes[k] = v
    return routes


def get_direct_flight_count(routes: dict, ap: str, dest: str) -> int | None:
    """获取某航线的直飞班次数；返回 None 表示该航线未查询过（数据缺失，不能判定为无直飞）"""
    key = f"{ap}-{dest}"
    if key in routes:
        data = routes[key]
        if isinstance(data, list):
            return len(data)
        return 0
    return None  # 数据缺失，保持原状态


# ============== HTML 解析：卡片信息提取 ==============
def extract_card_info(card_html: str, tab_ap: str) -> dict:
    """从 dest-card HTML 提取关键信息"""
    info = {"tab": tab_ap}

    # 城市代码
    m = re.search(r'<div class="dest-code">([A-Z0-9]{3})</div>', card_html)
    info["dest_code"] = m.group(1) if m else ""

    # 城市名
    m = re.search(r'<div class="dest-city">([^<]+)</div>', card_html)
    info["dest_city"] = m.group(1) if m else ""

    # 国旗emoji
    m = re.search(r'<span class="dest-flag">([^<]+)</span>', card_html)
    info["flag"] = m.group(1) if m else ""

    # 价格
    m = re.search(r'<span class="price-badge"[^>]*>(¥?[\d,]+)</span>', card_html)
    if m:
        price_str = m.group(1).replace("¥", "").replace(",", "")
        info["price"] = int(price_str) if price_str.isdigit() else None
    else:
        info["price"] = None

    # 直飞角标
    m = re.search(r'<span class="direct-badge[^"]*">([^<]+)</span>', card_html)
    info["direct_badge"] = m.group(1).strip() if m else ""

    # data-search
    m = re.search(r'data-search="([^"]+)"', card_html)
    info["data_search"] = m.group(1) if m else ""

    # 航班信息行
    m = re.search(r'<div class="flight-line">([^<]*)</div>', card_html)
    info["flight_line"] = m.group(1).strip() if m else ""

    return info


def update_card_price(card_html: str, new_price: int, flight_no: str = "", dep_time: str = "") -> str:
    """原地更新卡片中的价格标签"""
    price_str = f"¥{new_price}"
    # 更新 price-badge 内容
    new_html = re.sub(
        r'(<span class="price-badge"[^>]*>)([^<]+)(</span>)',
        lambda m: m.group(1) + price_str + m.group(3),
        card_html,
        count=1,
    )
    # 更新flight-line（如果有新航班信息）
    if flight_no:
        flight_text = f"✈ {flight_no}"
        if dep_time:
            flight_text += f" {dep_time}"
        new_html = re.sub(
            r'(<div class="flight-line">)([^<]*)(</div>)',
            lambda m: m.group(1) + flight_text + m.group(3),
            new_html,
            count=1,
        )
    return new_html


def update_card_direct_badge(card_html: str, count: int) -> str:
    """更新直飞角标"""
    badge_text = f"直飞{count}班" if count > 0 else "暂无直飞"
    badge_class = "direct-badge " if count > 0 else "direct-badge transfer"
    new_html = re.sub(
        r'<span class="direct-badge[^"]*">[^<]+</span>',
        f'<span class="{badge_class}">{badge_text}</span>',
        card_html,
        count=1,
    )
    return new_html


# ============== 卡片增删 ==============
def find_cards_in_tab(html: str, tab_id: str) -> list[tuple[int, int, str, str]]:
    """找到指定 tab 内所有 dest-card，返回 [(start, end, text, region)]"""
    # 先找 tab 边界
    tab_match = find_outermost_div_by_id(html, f"tab-{tab_id}")
    if not tab_match:
        return []
    tab_start, tab_end = tab_match
    tab_html = html[tab_start:tab_end]

    # 找所有 region-section
    regions = find_div_blocks(tab_html, "region-section")
    cards = []
    for r_start, r_end, r_text in regions:
        # 提取 region 名
        m = re.search(r'data-region="([^"]+)"', r_text)
        region = m.group(1) if m else ""
        # 找 region 内的 dest-card
        inner_cards = find_div_blocks(r_text, "dest-card")
        for c_start, c_end, c_text in inner_cards:
            # 绝对位置 = tab_start + r_start + c_start
            abs_start = tab_start + r_start + c_start
            abs_end = tab_start + r_start + c_end
            cards.append((abs_start, abs_end, c_text, region))
    return cards


def find_region_blocks_in_tab(html: str, tab_id: str) -> list[tuple[int, int, str, str]]:
    """找到指定 tab 内所有 region-section，返回 [(start, end, text, region_name)]"""
    tab_match = find_outermost_div_by_id(html, f"tab-{tab_id}")
    if not tab_match:
        return []
    tab_start, tab_end = tab_match
    tab_html = html[tab_start:tab_end]

    regions = find_div_blocks(tab_html, "region-section")
    result = []
    for r_start, r_end, r_text in regions:
        m = re.search(r'data-region="([^"]+)"', r_text)
        region = m.group(1) if m else ""
        abs_start = tab_start + r_start
        abs_end = tab_start + r_end
        result.append((abs_start, abs_end, r_text, region))
    return result


def hide_empty_regions(html: str, tab_id: str) -> tuple[str, int]:
    """隐藏空的 region-section（cards-grid 内没有 dest-card）"""
    regions = find_region_blocks_in_tab(html, tab_id)
    hidden = 0
    # 从后往前处理，避免位移
    for start, end, text, region in reversed(regions):
        if 'class="dest-card"' not in text:
            # 空区：加 display:none
            if 'style="' in text and 'display:none' not in text:
                new_text = re.sub(
                    r'(<div class="region-section"[^>]*?)style="([^"]*)"',
                    lambda mm: f'{mm.group(1)}style="display:none;{mm.group(2)}"',
                    text,
                    count=1,
                )
            elif 'style="' not in text:
                new_text = text.replace(
                    '<div class="region-section"',
                    '<div class="region-section" style="display:none"',
                    1,
                )
            else:
                new_text = text
            if new_text != text:
                html = html[:start] + new_text + html[end:]
                hidden += 1
        else:
            # 非空区：确保 display:none 被移除
            if re.search(r'<div class="region-section"[^>]*style="[^"]*display:none', text):
                new_text = re.sub(
                    r'(<div class="region-section"[^>]*?)style="[^"]*display:none;?([^"]*)"',
                    lambda mm: f'{mm.group(1)}style="{mm.group(2)}"' if mm.group(2) else f'{mm.group(1)}',
                    text,
                    count=1,
                )
                # 清理空 style
                new_text = re.sub(r'\s+style=""', "", new_text)
                html = html[:start] + new_text + html[end:]
    return html, hidden


# ============== 新建卡片 HTML 模板 ==============
def build_new_card_html(
    ap: str,
    dest_code: str,
    dest_city: str,
    price: int,
    direct_count: int,
    variflight_data: list | None,
    flag: str = "",
) -> str:
    """构造一张新的 dest-card HTML"""
    # 生成航班信息行
    flight_line = ""
    detail_rows = ""
    if variflight_data and len(variflight_data) > 0:
        first = variflight_data[0]
        fno = first.get("flight_no", "")
        dep = first.get("dep_time", "")
        # 提取 HH:MM
        dep_time = ""
        if dep:
            m = re.search(r"(\d{1,2}:\d{2})", dep)
            dep_time = m.group(1) if m else ""
        if fno:
            flight_line = f"✈ {fno}"
            if dep_time:
                flight_line += f" {dep_time}"

        # 构造 detail-panel 表格行
        for f in variflight_data[:6]:
            fno = f.get("flight_no", "")
            airline = f.get("airline", "")
            dep_t = ""
            arr_t = ""
            if f.get("dep_time"):
                m = re.search(r"(\d{1,2}:\d{2})", f["dep_time"])
                dep_t = m.group(1) if m else ""
            if f.get("arr_time"):
                m = re.search(r"(\d{1,2}:\d{2})", f["arr_time"])
                arr_t = m.group(1) if m else ""
            aircraft = f.get("aircraft", "")
            ontime = f.get("ontime_rate", "")
            checkin = f.get("check_in", "")
            check_door = f.get("check_door", "")

            detail_rows += (
                f"<tr><td>{fno}</td><td>{airline}</td><td>{dep_t}</td>"
                f"<td>{arr_t}</td><td>{aircraft}</td><td>{ontime}</td>"
                f"<td>{checkin}</td><td>{check_door}</td></tr>"
            )

    badge_class = "direct-badge " if direct_count > 0 else "direct-badge transfer"
    badge_text = f"直飞{direct_count}班" if direct_count > 0 else "暂无直飞"

    search_text = f"{dest_city} {dest_code} {flag}"

    # 购票链接
    book_link = f"https://www.fliggy.com/flight/international-search?depCity={ap}&arrCity={dest_code}&depDate=2026-09-10"

    detail_panel = ""
    if detail_rows:
        detail_panel = f'''<div id="detail-{ap}-{dest_code}" class="detail-panel" style="display:none"><h4>航班列表 <small>({direct_count}个航班)</small></h4><div class="table-wrap"><table class="flight-table"><tr><th>航班号</th><th>航司</th><th>起飞</th><th>到达</th><th>机型</th><th>准点率</th><th>值机</th><th>值机门</th></tr>{detail_rows}</table></div></div>'''

    card = f'''<div class="dest-card" onclick="openModal(this)" data-search="{search_text}"><div class="card-header"><div class="card-left"><span class="dest-flag">{flag}</span><div><div class="dest-city">{dest_city}</div><div class="dest-code">{dest_code}</div></div></div><div class="card-right"><div class="price-area"><span class="price-badge" style="background:#b8860b18;color:#b8860b">¥{price}</span><span class="price-label" style="color:#b8860b">TripNow价</span></div><span class="{badge_class}">{badge_text}</span></div></div>'''

    if flight_line:
        card += f'<div class="flight-line">{flight_line}</div>'

    card += f'''<div class="card-actions"><a href="{book_link}" target="_blank" rel="noopener noreferrer" class="btn-book">购票</a></div>{detail_panel}</div>'''

    return card


def add_card_to_region(html: str, tab_id: str, region_name: str, card_html: str) -> str:
    """在指定 tab 的指定 region 的 cards-grid 末尾添加卡片"""
    regions = find_region_blocks_in_tab(html, tab_id)
    for r_start, r_end, r_text, r_name in regions:
        if r_name == region_name:
            # 找到 cards-grid
            grid_match = re.search(r'(<div class="cards-grid">)', r_text)
            if grid_match:
                # 找到 cards-grid 的闭合位置
                grid_start_in_region = grid_match.end()
                # 从 grid_start 开始找配对
                depth = 1
                k = grid_start_in_region
                while k < len(r_text):
                    if re.match(r"<div\b", r_text[k:], re.IGNORECASE):
                        depth += 1
                        k += 4
                    elif re.match(r"</div>", r_text[k:], re.IGNORECASE):
                        depth -= 1
                        k += 6
                        if depth == 0:
                            break
                    else:
                        k += 1
                # 在 grid 闭合前插入新卡片
                insert_pos_in_region = grid_start_in_region + (k - grid_start_in_region) - 6  # -6 to insert before </div>
                abs_insert = r_start + insert_pos_in_region
                html = html[:abs_insert] + card_html + html[abs_insert:]
                # 如果该 region 是 display:none，需要移除
                if re.search(r'<div class="region-section"[^>]*style="[^"]*display:none', r_text):
                    # 重新获取该 region 块（位置已变，近似处理）
                    pass
                return html
            break
    return html


def make_region_visible(html: str, tab_id: str, region_name: str) -> str:
    """确保指定 region 不是 display:none"""
    regions = find_region_blocks_in_tab(html, tab_id)
    for r_start, r_end, r_text, r_name in regions:
        if r_name == region_name:
            if re.search(r'<div class="region-section"[^>]*style="[^"]*display:none', r_text):
                new_text = re.sub(
                    r'(<div class="region-section"[^>]*?)style="[^"]*display:none;?([^"]*)"',
                    lambda mm: f'{mm.group(1)}style="{mm.group(2)}"' if mm.group(2) else f'{mm.group(1)}',
                    r_text,
                    count=1,
                )
                new_text = re.sub(r'\s+style=""', "", new_text)
                html = html[:r_start] + new_text + html[r_end:]
            break
    return html


# ============== 回旋镖处理 ==============
def update_boomerang_cards(html: str, prices_data: dict) -> tuple[str, dict]:
    """更新回旋镖卡片价格，超门槛移除，返回 (新html, 统计)"""
    stats = {"updated": 0, "removed": 0, "added": 0}

    # 从 _boomerang_totals 计算最新总价
    totals = prices_data.get("_boomerang_totals", {})
    legs = prices_data.get("_boomerang_legs", {})

    # 重新计算每个组合的总价
    recalculated = {}
    for combo_name, combo_info in totals.items():
        combo_legs = combo_info.get("legs", {})
        leg_prices = []
        all_ok = True
        for leg_key in combo_legs:
            leg_data = legs.get(leg_key, {})
            p = leg_data.get("price")
            if p is None:
                all_ok = False
                break
            leg_prices.append(p)
        total = sum(leg_prices) if all_ok else None
        recalculated[combo_name] = {
            "total": total,
            "legs": combo_legs,
            "all_ok": all_ok,
        }

    # 类型判断：两程/三程 对应不同门槛
    def get_threshold(combo_key: str, combo_info: dict) -> int:
        ctype = combo_info.get("type", "")
        if "2" in ctype:
            return PRICE_THRESHOLDS["boom_domestic_2"] if "domestic" in ctype else PRICE_THRESHOLDS["boom_intl_2"]
        if "3" in ctype:
            return PRICE_THRESHOLDS["boom_domestic_3"] if "domestic" in ctype else PRICE_THRESHOLDS["boom_intl_3"]
        # 兜底：按腿数判断，2程≤1000，3程≤1500
        leg_count = len(combo_info.get("legs", {}))
        if leg_count <= 2:
            return PRICE_THRESHOLDS.get("boom_domestic", 1000)
        return PRICE_THRESHOLDS.get("boom_domestic", 1500)

    # 找到 boomerang tab
    tab_match = find_outermost_div_by_id(html, "tab-boomerang")
    if not tab_match:
        return html, stats
    tab_start, tab_end = tab_match

    # 找到所有 boomerang-card
    tab_html = html[tab_start:tab_end]
    boom_cards = find_div_blocks(tab_html, "boomerang-card")

    # 从后往前处理
    for c_start, c_end, c_text in reversed(boom_cards):
        abs_start = tab_start + c_start
        abs_end = tab_start + c_end

        # 通过 data-card 属性直接匹配 combo key
        dc_match = re.search(r'data-card="([^"]+)"', c_text)
        card_key = dc_match.group(1) if dc_match else ""

        # 找到对应的 combo（精确匹配 key）
        if card_key not in recalculated:
            continue

        info = recalculated[card_key]
        total = info["total"]

        # 判断门槛
        threshold = get_threshold(card_key, totals.get(card_key, {}))

        if total is None:
            # 有航段价格缺失，移除
            html = html[:abs_start] + html[abs_end:]
            stats["removed"] += 1
        elif total > threshold:
            # 超门槛，移除
            html = html[:abs_start] + html[abs_end:]
            stats["removed"] += 1
        else:
            # 更新价格
            new_text = re.sub(
                r'(<span class="route-price">)总价 ¥\d+(</span>)',
                lambda mm: f'{mm.group(1)}总价 ¥{total}{mm.group(2)}',
                c_text,
                count=1,
            )
            if new_text != c_text:
                html = html[:abs_start] + new_text + html[abs_end:]
                stats["updated"] += 1

    # 同步区域显隐：根据 boomerang-routes 内卡片数量
    # 国内回旋区
    html = sync_boomerang_section_visibility(html)

    return html, stats


def sync_boomerang_section_visibility(html: str) -> str:
    """同步回旋镖各区域显隐（有卡显示，无卡隐藏）"""
    # 找到 tab-boomerang
    tab_match = find_outermost_div_by_id(html, "tab-boomerang")
    if not tab_match:
        return html
    tab_start, tab_end = tab_match
    tab_html = html[tab_start:tab_end]

    # 找所有 boomerang-routes
    routes = find_div_blocks(tab_html, "boomerang-routes")
    # 找到每个 routes 前的 h3 标题
    for r_start, r_end, r_text in routes:
        abs_start = tab_start + r_start
        abs_end = tab_start + r_end
        has_cards = 'class="boomerang-card"' in r_text

        # 更新该 routes 的 display
        if has_cards:
            if re.search(r'style="[^"]*display:none', r_text):
                new_text = re.sub(
                    r'style="[^"]*display:none;?([^"]*)"',
                    lambda mm: f'style="{mm.group(1)}"' if mm.group(1) else "",
                    r_text,
                    count=1,
                )
                new_text = re.sub(r'\s+style=""', "", new_text)
                html = html[:abs_start] + new_text + html[abs_end:]
        else:
            if 'style="' in r_text:
                if "display:none" not in r_text:
                    new_text = re.sub(
                        r'(<div class="boomerang-routes"[^>]*?)style="([^"]*)"',
                        lambda mm: f'{mm.group(1)}style="display:none;{mm.group(2)}"',
                        r_text,
                        count=1,
                    )
                    html = html[:abs_start] + new_text + html[abs_end:]
            else:
                new_text = r_text.replace(
                    '<div class="boomerang-routes"',
                    '<div class="boomerang-routes" style="display:none"',
                    1,
                )
                html = html[:abs_start] + new_text + html[abs_end:]

        # 同步前一个 h3 标题的显隐
        # 先找到更新后或原始的 abs_start，再往前找 h3
        current_r_start = html.find(r_text[:100], tab_start)
        if current_r_start > 0:
            prefix = html[tab_start:current_r_start]
            h3_matches = list(re.finditer(r"<h3[^>]*>", prefix))
            if h3_matches:
                last_h3 = h3_matches[-1]
                h3_start = tab_start + last_h3.start()
                # 找到 h3 结束
                h3_end_match = re.search(r"</h3>", html[h3_start:])
                if h3_end_match:
                    h3_end = h3_start + h3_end_match.end()
                    h3_html = html[h3_start:h3_end]
                    if has_cards:
                        if "display:none" in h3_html:
                            new_h3 = re.sub(
                                r'style="[^"]*display:none;?([^"]*)"',
                                lambda mm: f'style="{mm.group(1)}"' if mm.group(1) else "",
                                h3_html,
                                count=1,
                            )
                            new_h3 = re.sub(r'\s+style=""', "", new_h3)
                            html = html[:h3_start] + new_h3 + html[h3_end:]
                    else:
                        if "display:none" not in h3_html:
                            if 'style="' in h3_html:
                                new_h3 = re.sub(
                                    r'(<h3[^>]*?)style="([^"]*)"',
                                    lambda mm: f'{mm.group(1)}style="display:none;{mm.group(2)}"',
                                    h3_html,
                                    count=1,
                                )
                            else:
                                new_h3 = h3_html.replace("<h3", '<h3 style="display:none"', 1)
                            html = html[:h3_start] + new_h3 + html[h3_end:]
    return html


# ============== 主逻辑 ==============
async def main():
    # 参数解析
    result_mode = sys.argv[1] if len(sys.argv) > 1 else "display_only"
    html_path = sys.argv[2] if len(sys.argv) > 2 else "/Coze/Drive/扣子2/特价航班查询/全球特价航班速查_2026年9月.html"
    work_dir = sys.argv[3] if len(sys.argv) > 3 else "/Coze/Drive/扣子2/特价航班查询"
    validate_script = sys.argv[4] if len(sys.argv) > 4 else "/Coze/Drive/扣子2/.skills/skill_create-html-artifact/scripts/validate_html.py"

    sdk = CodeActSDK()
    actual_mode = result_mode if result_mode != "auto" else "display_only"

    try:
        print(f"[开始] 每日航班价格刷新 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"[参数] HTML路径: {html_path}")
        print(f"[参数] 工作目录: {work_dir}")

        # 1. 读取现有价格数据（作为旧价和航线清单）
        prices_path = os.path.join(work_dir, "tripnow_prices_v3.json")
        if not os.path.exists(prices_path):
            raise FileNotFoundError(f"价格数据文件不存在: {prices_path}")

        with open(prices_path, encoding="utf-8") as f:
            old_prices = json.load(f)

        print(f"[数据] 旧价格已加载，直飞航线数: {sum(len(old_prices.get(ap, {}).get(cat, {})) for ap in ['CZX','WUX','SHA','PVG'] for cat in ['domestic','intl'])}")

        # 2. 读取飞常准数据
        variflight_routes = load_variflight_routes(work_dir)
        print(f"[数据] 飞常准航线数据: {len(variflight_routes)} 条，其中有直飞 {sum(1 for v in variflight_routes.values() if v and isinstance(v, list) and len(v) > 0)} 条")

        # 3. 构建查询任务列表
        # 策略：每日全量查询，但为控制总耗时，对"明显远低于门槛"的航线跳过
        # （国内≤¥400、国际≤¥700的航线单日涨价不会突破门槛），
        # 重点查询门槛附近航线、失败航线、回旋镖航段。
        # 若 old_prices 文件不存在则全量查询。
        tasks = []  # [(kind, ap, dest, cat, orig_city, dest_city, tag)]
        skipped_stable = 0

        for ap in ["CZX", "WUX", "SHA", "PVG"]:
            if ap not in old_prices:
                continue
            for cat in ["domestic", "intl"]:
                if cat not in old_prices[ap]:
                    continue
                threshold = PRICE_THRESHOLDS[cat]
                # 稳定低价阈值：国内 ≤400、国际/港澳台 ≤700
                stable_threshold = 400 if cat == "domestic" else 700
                for dest_code in old_prices[ap][cat]:
                    info = old_prices[ap][cat][dest_code]
                    orig_city = AP_NAMES.get(ap, ap)
                    dest_city = CITY_NAMES.get(dest_code, dest_code)
                    price = info.get("price")
                    status = info.get("status", "ok")

                    # 必须查询：失败航线、无价格、价格接近或超过门槛
                    must_query = (
                        status == "failed_keep_old"
                        or price is None
                        or price > stable_threshold
                    )
                    if must_query:
                        tasks.append(("direct", ap, dest_code, cat, orig_city, dest_city, f"{ap}-{dest_code}"))
                    else:
                        skipped_stable += 1
                        # 稳定低价航线保留旧价

        # 回旋镖航段：全部查询
        boom_legs = old_prices.get("_boomerang_legs", {})
        for leg_key in boom_legs:
            parts = leg_key.split("-")
            if len(parts) == 2:
                orig, dest = parts
                orig_city = CITY_NAMES.get(orig, orig)
                dest_city = CITY_NAMES.get(dest, dest)
                tasks.append(("boom_leg", orig, dest, "", orig_city, dest_city, leg_key))

        total_routes = sum(
            len(old_prices.get(ap, {}).get(cat, {}))
            for ap in ["CZX", "WUX", "SHA", "PVG"]
            for cat in ["domestic", "intl"]
        )
        print(f"[查询] 直飞航线总数: {total_routes}")
        print(f"[查询] 跳过稳定低价: {skipped_stable} 条（保留旧价）")
        print(f"[查询] 待查询: {len(tasks)} 条（直飞{len([t for t in tasks if t[0]=='direct'])} + 回旋航段{len([t for t in tasks if t[0]=='boom_leg'])}）")

        # 4. 批量查询 TripNow 价格
        new_prices = {}  # 深拷贝旧数据结构
        for ap in ["CZX", "WUX", "SHA", "PVG"]:
            new_prices[ap] = {}
            for cat in ["domestic", "intl"]:
                if ap in old_prices and cat in old_prices[ap]:
                    new_prices[ap][cat] = {}
                    for dest, info in old_prices[ap][cat].items():
                        new_prices[ap][cat][dest] = dict(info)  # 复制旧数据
        new_prices["_boomerang_legs"] = dict(old_prices.get("_boomerang_legs", {}))
        new_prices["_boomerang_totals"] = json.loads(json.dumps(old_prices.get("_boomerang_totals", {})))

        failed_routes = []
        updated_routes = []  # [(tag, old_price, new_price)]

        for idx, (kind, ap, dest, cat, orig_city, dest_city, tag) in enumerate(tasks):
            print(f"[{idx+1}/{len(tasks)}] {tag}: {orig_city} → {dest_city}", end="", flush=True)

            prompt = f"{orig_city}到{dest_city} 2026年9月下旬 最便宜机票价格"
            t_start = time.time()
            content = tripnow_query(prompt)
            t_elapsed = time.time() - t_start

            price = parse_price(content)
            fno, dtime = parse_flight_info(content or "")

            if price is None:
                print(f" → 查询失败，保留旧价", flush=True)
                failed_routes.append(tag)
                # 保留旧价，标记状态
                if kind == "direct":
                    if ap in new_prices and cat in new_prices[ap] and dest in new_prices[ap][cat]:
                        new_prices[ap][cat][dest]["status"] = "failed_keep_old"
                else:
                    if tag in new_prices["_boomerang_legs"]:
                        new_prices["_boomerang_legs"][tag]["status"] = "failed_keep_old"
            else:
                old_price = None
                if kind == "direct":
                    if ap in old_prices and cat in old_prices[ap] and dest in old_prices[ap][cat]:
                        old_price = old_prices[ap][cat][dest].get("price")
                    new_prices[ap][cat][dest] = {
                        "price": price,
                        "old_price": old_price,
                        "flight_no": fno,
                        "dep_time": dtime,
                        "status": "ok",
                    }
                else:
                    if tag in old_prices.get("_boomerang_legs", {}):
                        old_price = old_prices["_boomerang_legs"][tag].get("price")
                    new_prices["_boomerang_legs"][tag] = {
                        "price": price,
                        "flight_no": fno,
                        "dep_time": dtime,
                        "status": "ok",
                        "source": "api",
                    }

                if old_price and old_price != price:
                    print(f" → ¥{price} (旧¥{old_price})", flush=True)
                    updated_routes.append((tag, old_price, price))
                else:
                    print(f" → ¥{price}", flush=True)

            # 限流间隔：确保两次请求间隔至少 1.5s（扣除已消耗的请求时间）
            if idx < len(tasks) - 1:
                remaining = 1.5 - t_elapsed
                if remaining > 0:
                    time.sleep(remaining)

        # 重新计算回旋镖总价
        boom_totals = new_prices.get("_boomerang_totals", {})
        boom_legs_data = new_prices.get("_boomerang_legs", {})
        for combo_name, combo_info in boom_totals.items():
            combo_legs = combo_info.get("legs", {})
            total = 0
            all_ok = True
            for leg_key in combo_legs:
                leg_info = boom_legs_data.get(leg_key, {})
                p = leg_info.get("price")
                if p is None:
                    all_ok = False
                    break
                total += p
            combo_info["total"] = total if all_ok else None

        # 5. 保存新价格数据
        with open(prices_path, "w", encoding="utf-8") as f:
            json.dump(new_prices, f, ensure_ascii=False, indent=2)
        print(f"[保存] 新价格已写入 {prices_path}")

        # 6. 备份 HTML
        backup_path = os.path.join(
            work_dir,
            f"全球特价航班速查_2026年9月.bak_{datetime.now().strftime('%Y%m%d_%H%M')}.html",
        )
        with open(html_path, encoding="utf-8") as f:
            html = f.read()
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[备份] 原HTML已备份到: {backup_path}")

        # 7. 处理每个机场 tab 的卡片
        stats = {
            "price_updated": 0,        # 更新了价格的卡片
            "removed_over_threshold": 0,  # 超门槛移除
            "removed_no_direct": 0,       # 无直飞移除
            "fixed_badge": 0,             # 修正误标角标
            "added_new": 0,               # 新增低价卡
            "hidden_regions": 0,          # 隐藏的空区域
            "failed_routes": failed_routes,
        }

        # 收集变化信息用于简报
        price_drops = []  # [(route, old, new)]
        price_rises = []  # [(route, old, new)]
        new_low_routes = []  # [route] 新入榜（原来超门槛现在低于门槛）
        removed_routes = []  # [(route, old_price, reason)]

        for ap in ["CZX", "WUX", "SHA", "PVG"]:
            print(f"\n[处理] tab-{ap}...", flush=True)

            # 获取该机场所有卡片
            cards = find_cards_in_tab(html, ap)

            # 构建 card key → position 映射
            card_map = {}
            for c_start, c_end, c_text, region in cards:
                info = extract_card_info(c_text, ap)
                dest_code = info["dest_code"]
                if dest_code:
                    card_map[dest_code] = {
                        "start": c_start,
                        "end": c_end,
                        "text": c_text,
                        "region": region,
                        "info": info,
                    }

            # 获取该机场的所有航线价格数据
            ap_data = new_prices.get(ap, {})

            # 遍历所有价格数据，决定每个航线的处理
            all_dests = set()
            for cat in ["domestic", "intl"]:
                all_dests.update(ap_data.get(cat, {}).keys())

            # 从后往前删除，避免位移
            dests_to_remove = []  # (dest_code, reason)

            for dest_code in all_dests:
                # 找所属分类
                cat = dest_category(dest_code)
                if cat == "domestic":
                    price_info = ap_data.get("domestic", {}).get(dest_code) or ap_data.get("intl", {}).get(dest_code)
                else:
                    price_info = ap_data.get("intl", {}).get(dest_code) or ap_data.get("domestic", {}).get(dest_code)

                if not price_info:
                    continue

                price = price_info.get("price")
                old_price = price_info.get("old_price")
                threshold = PRICE_THRESHOLDS[cat]
                direct_count = get_direct_flight_count(variflight_routes, ap, dest_code)

                if dest_code in card_map:
                    # 卡片已存在
                    card_entry = card_map[dest_code]

                    # 判断是否需要移除
                    should_remove = False
                    remove_reason = ""

                    # 无直飞则删除（只有飞常准确认过且为0班才删，数据缺失的不删）
                    if direct_count == 0:
                        should_remove = True
                        remove_reason = "无直飞"
                    # 超价格门槛则删除
                    elif price and price > threshold:
                        should_remove = True
                        remove_reason = "超门槛"

                    if should_remove:
                        dests_to_remove.append((dest_code, remove_reason, price or old_price or 0))
                        removed_routes.append((f"{ap}-{dest_code}", price or old_price, remove_reason))
                    else:
                        # 更新价格
                        old_card_price = card_entry["info"].get("price")
                        new_card_html = card_entry["text"]

                        if price and price != old_card_price:
                            fno = price_info.get("flight_no", "")
                            dtime = price_info.get("dep_time", "")
                            new_card_html = update_card_price(new_card_html, price, fno, dtime)
                            stats["price_updated"] += 1

                            if old_card_price and price < old_card_price:
                                price_drops.append((f"{ap}-{dest_code}", old_card_price, price))
                            elif old_card_price and price > old_card_price:
                                price_rises.append((f"{ap}-{dest_code}", old_card_price, price))

                        # 修正直飞角标（只有飞常准有数据时才修正）
                        if direct_count is not None and direct_count > 0:
                            current_badge = card_entry["info"].get("direct_badge", "")
                            expected_badge = f"直飞{direct_count}班"
                            if current_badge != expected_badge:
                                new_card_html = update_card_direct_badge(new_card_html, direct_count)
                                stats["fixed_badge"] += 1

                        # 写回
                        if new_card_html != card_entry["text"]:
                            html = html[:card_entry["start"]] + new_card_html + html[card_entry["end"]:]
                else:
                    # 卡片不存在，检查是否需要新增
                    # 新增条件：价格达标 + 飞常准确认有直飞（或飞常准无数据时不新增，避免假新增）
                    if price and price <= threshold and direct_count is not None and direct_count > 0:
                        # 新增卡片
                        dest_city = CITY_NAMES.get(dest_code, dest_code)
                        region_name = dest_to_region(dest_code)
                        # 国旗
                        flag = ""
                        if dest_code in REGION_DESTS["domestic"]:
                            flag = "🇨🇳"
                        elif dest_code in REGION_DESTS["intl_sea"]:
                            sea_flags = {"BKK": "🇹🇭", "SIN": "🇸🇬", "KUL": "🇲🇾", "SGN": "🇻🇳", "DPS": "🇮🇩", "MNL": "🇵🇭"}
                            flag = sea_flags.get(dest_code, "")
                        elif dest_code in REGION_DESTS["intl_je"]:
                            je_flags = {"ICN": "🇰🇷", "HND": "🇯🇵", "KIX": "🇯🇵"}
                            flag = je_flags.get(dest_code, "")
                        elif dest_code in REGION_DESTS["intl_hkmt"]:
                            hkmt_flags = {"HKG": "🇭🇰", "MFM": "🇲🇴", "TPE": "🇹🇼"}
                            flag = hkmt_flags.get(dest_code, "")

                        # 获取飞常准航班数据用于 detail-panel
                        vf_data = variflight_routes.get(f"{ap}-{dest_code}")

                        new_card = build_new_card_html(
                            ap, dest_code, dest_city, price, direct_count, vf_data, flag
                        )
                        html = add_card_to_region(html, ap, region_name, new_card)
                        html = make_region_visible(html, ap, region_name)
                        stats["added_new"] += 1
                        new_low_routes.append(f"{ap}-{dest_code} (¥{price})")

            # 执行删除（从后往前）
            if dests_to_remove:
                # 重新获取最新的卡片位置
                cards = find_cards_in_tab(html, ap)
                card_map = {}
                for c_start, c_end, c_text, region in cards:
                    info = extract_card_info(c_text, ap)
                    dest_code = info["dest_code"]
                    if dest_code:
                        card_map[dest_code] = {"start": c_start, "end": c_end}

                # 按位置从后往前删除
                remove_list = []
                for d, reason, _ in dests_to_remove:
                    if d in card_map:
                        remove_list.append((card_map[d]["start"], card_map[d]["end"], d, reason))
                remove_list.sort(key=lambda x: x[0], reverse=True)

                for start, end, dest_code, reason in remove_list:
                    html = html[:start] + html[end:]
                    if reason == "超门槛":
                        stats["removed_over_threshold"] += 1
                    elif reason == "无直飞":
                        stats["removed_no_direct"] += 1

            # 处理空区域隐藏
            html, hidden = hide_empty_regions(html, ap)
            stats["hidden_regions"] += hidden
            print(f"  价格更新: {stats['price_updated']}, 新增: {stats['added_new']}, "
                  f"移除(超门槛): {stats['removed_over_threshold']}, 移除(无直飞): {stats['removed_no_direct']}, "
                  f"修正角标: {stats['fixed_badge']}, 隐藏空区域: {stats['hidden_regions']}", flush=True)

        # 8. 处理回旋镖
        print("\n[处理] 回旋镖航线...", flush=True)
        html, boom_stats = update_boomerang_cards(html, new_prices)
        print(f"  回旋镖更新: {boom_stats['updated']}, 移除: {boom_stats['removed']}", flush=True)

        # 9. 更新数据更新日期
        today_str = datetime.now().strftime("%Y-%m-%d")
        html = re.sub(
            r"🔄 数据更新：\d{4}-\d{2}-\d{2}",
            f"🔄 数据更新：{today_str}",
            html,
            count=1
        )
        print(f"  数据更新日期已更新为: {today_str}", flush=True)

        # 10. 写入更新后的 HTML
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n[写入] HTML已更新，文件大小: {len(html.encode('utf-8'))} 字节")

        # 10. 校验 HTML
        print("\n[校验] 运行 validate_html.py ...", flush=True)
        try:
            result = subprocess.run(
                ["python3", validate_script, html_path],
                capture_output=True,
                text=True,
                timeout=60,
            )
            validate_output = result.stdout + result.stderr
            # 提取 errors 数量（格式: errors=0）
            err_match = re.search(r"errors=(\d+)", validate_output)
            warn_match = re.search(r"warnings=(\d+)", validate_output)
            errors_count = int(err_match.group(1)) if err_match else -1
            warnings_count = int(warn_match.group(1)) if warn_match else -1
            print(f"  校验结果: errors={errors_count}, warnings={warnings_count}")
            if errors_count > 0:
                print(f"  校验输出最后300字符:\n{validate_output[-300:]}")
        except Exception as e:
            errors_count = -1
            warnings_count = -1
            validate_output = f"校验执行失败: {e}"
            print(f"  {validate_output}")

        # 11. 生成简报
        # 排序：降价幅度从大到小
        price_drops.sort(key=lambda x: x[1] - x[2], reverse=True)
        price_rises.sort(key=lambda x: x[2] - x[1], reverse=True)

        # 构造简报
        today_str = datetime.now().strftime("%Y-%m-%d")
        lines = [f"🛫 特价航班每日简报 | {today_str}", ""]

        # 核心指标
        total_cards_before = sum(
            1 for ap in ["CZX", "WUX", "SHA", "PVG"]
            for cat in ["domestic", "intl"]
            for dest, info in old_prices.get(ap, {}).get(cat, {}).items()
            if info.get("price") and info["price"] <= PRICE_THRESHOLDS[cat]
        )
        total_cards_after = stats["price_updated"] + len(card_map) if False else 0  # placeholder

        lines.append(f"**价格更新**：{stats['price_updated']} 条航线价格有变动")
        lines.append(f"**新增低价**：{stats['added_new']} 条")
        lines.append(f"**移除航线**：{stats['removed_over_threshold']} 条超门槛 + {stats['removed_no_direct']} 条无直飞")
        lines.append(f"**角标修正**：{stats['fixed_badge']} 条误标")
        lines.append(f"**空区隐藏**：{stats['hidden_regions']} 个区域")
        lines.append("")

        if price_drops:
            lines.append("📉 **降价 TOP（前10条）**")
            for route, old, new in price_drops[:10]:
                drop = old - new
                pct = int(drop / old * 100) if old else 0
                lines.append(f"  · {route}：¥{old} → ¥{new}（↓¥{drop}, -{pct}%）")
            lines.append("")

        if price_rises:
            lines.append("📈 **涨价 TOP（前10条）**")
            for route, old, new in price_rises[:10]:
                rise = new - old
                pct = int(rise / old * 100) if old else 0
                lines.append(f"  · {route}：¥{old} → ¥{new}（↑¥{rise}, +{pct}%）")
            lines.append("")

        if new_low_routes:
            lines.append("✨ **新增低价航线**")
            for r in new_low_routes[:10]:
                lines.append(f"  · {r}")
            lines.append("")

        if removed_routes:
            lines.append("🚫 **移除航线**")
            for route, price, reason in removed_routes[:10]:
                lines.append(f"  · {route}（¥{price}, {reason}）")
            lines.append("")

        if failed_routes:
            lines.append(f"⚠️ **查询失败**（{len(failed_routes)}条，保留旧价）：")
            lines.append(f"  {', '.join(failed_routes[:8])}")
            if len(failed_routes) > 8:
                lines.append(f"  等共 {len(failed_routes)} 条")
            lines.append("")

        # 校验结果
        validate_status = "✅ 通过" if errors_count == 0 else ("❌ 失败" if errors_count > 0 else "⚠️ 未知")
        lines.append(f"**HTML校验**：{validate_status}（errors={errors_count}, warnings={warnings_count}）")
        lines.append("")
        lines.append(f"更新文件：[全球特价航班速查_2026年9月.html](computer://{os.path.abspath(html_path)})")

        message = "\n".join(lines)

        # 简报也保存一份到 output
        os.makedirs("./codeact/output", exist_ok=True)
        briefing_path = f"./codeact/output/flight_briefing_{datetime.now().strftime('%Y%m%d')}.md"
        with open(briefing_path, "w", encoding="utf-8") as f:
            f.write(message)

        # 12. 提交结果
        data = {
            "updated_count": stats["price_updated"],
            "added_count": stats["added_new"],
            "removed_over_threshold": stats["removed_over_threshold"],
            "removed_no_direct": stats["removed_no_direct"],
            "fixed_badge_count": stats["fixed_badge"],
            "hidden_regions": stats["hidden_regions"],
            "failed_routes": failed_routes,
            "validate_errors": errors_count,
            "validate_warnings": warnings_count,
            "html_path": html_path,
            "backup_path": backup_path,
            "briefing_path": briefing_path,
            "boomerang_updated": boom_stats["updated"],
            "boomerang_removed": boom_stats["removed"],
        }

        await sdk.submit_result(
            result_mode=actual_mode,
            status="success",
            message=message,
            data=data,
        )

    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"[错误] {error_detail}", flush=True)
        await sdk.submit_result(
            result_mode="notify",
            status="error",
            message=f"每日航班刷新失败: {str(e)}",
            data={"error_type": type(e).__name__},
        )


if __name__ == "__main__":
    asyncio.run(main())
