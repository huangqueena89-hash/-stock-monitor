#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股股票监控脚本 - 由 GitHub Actions 每小时触发
数据源: 新浪财经（实时行情）+ 腾讯财经（历史量能）+ 东方财富（资金流向，备用）
"""

import os
import re
import json
import requests
import urllib3
import datetime
import time

urllib3.disable_warnings()

SENDKEY = os.environ.get("SERVERCHAN_KEY", "SCT327260TTKdejkQDKqgqxbZCSNVB86pn")
STOCKS = {
    "600745": "闻泰科技",
    "600105": "永鼎股份",
    "600089": "特变电工",
    "001301": "尚太科技",
}
SERVER_CHAN_URL = f"https://sctapi.ftqq.com/{SENDKEY}.send"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
})


def now_cst():
    cst = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(cst).strftime("%Y-%m-%d %H:%M")


def send_wechat(title: str, content: str):
    try:
        resp = requests.post(SERVER_CHAN_URL, data={"title": title, "desp": content}, timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            print(f"[{now_cst()}] 微信推送成功")
        else:
            print(f"[{now_cst()}] 推送失败: {result}")
    except Exception as e:
        print(f"[{now_cst()}] 推送异常: {e}")


# ──────────────────────────────────────────
# 数据源1: 新浪财经 - 实时行情
# ──────────────────────────────────────────
def get_realtime_quotes(codes: list) -> dict:
    """批量获取实时行情，返回 {code: {...}} 字典"""
    symbols = [("sh" if c.startswith("6") else "sz") + c for c in codes]
    url = "https://hq.sinajs.cn/list=" + ",".join(symbols)
    try:
        resp = SESSION.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
        resp.encoding = "gbk"
        results = {}
        for line in resp.text.strip().split("\n"):
            m = re.match(r'var hq_str_[a-z]{2}(\d+)="(.*)";', line)
            if not m:
                continue
            code = m.group(1)
            fields = m.group(2).split(",")
            if len(fields) < 32 or not fields[3]:
                continue
            prev_close = float(fields[2]) if fields[2] else 0
            price      = float(fields[3]) if fields[3] else 0
            change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0
            change_amt = round(price - prev_close, 2)
            results[code] = {
                "name":         fields[0],
                "open":         float(fields[1]) if fields[1] else 0,
                "prev_close":   prev_close,
                "price":        price,
                "high":         float(fields[4]) if fields[4] else 0,
                "low":          float(fields[5]) if fields[5] else 0,
                "volume":       float(fields[8]) / 100 if fields[8] else 0,  # 转换为手
                "amount":       float(fields[9]) if fields[9] else 0,
                "change_pct":   change_pct,
                "change_amt":   change_amt,
            }
        return results
    except Exception as e:
        print(f"  [新浪行情] 获取失败: {e}")
        return {}


# ──────────────────────────────────────────
# 数据源2: 腾讯财经 - 历史K线（计算均量）
# ──────────────────────────────────────────
def get_hist_volumes(code: str, count: int = 15) -> list:
    """获取近N日成交量（手），用于计算5日/10日均量"""
    prefix = "sh" if code.startswith("6") else "sz"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"_var": "kline_day", "param": f"{prefix}{code},day,,,{count + 5},"}
    try:
        resp = SESSION.get(url, params=params, timeout=10)
        m = re.search(r'=(\{.*\})', resp.text, re.DOTALL)
        if not m:
            return []
        data = json.loads(m.group(1))
        key = f"{prefix}{code}"
        days = data.get("data", {}).get(key, {}).get("day", [])
        # 每条格式: [date, open, close, high, low, volume, ...]
        # volume 单位是手
        return [float(d[5]) for d in days if len(d) > 5]
    except Exception as e:
        print(f"  [腾讯历史] {code} 获取失败: {e}")
        return []


def get_volume_signal(code: str, today_vol: float) -> tuple:
    hist = get_hist_volumes(code, 15)
    if len(hist) < 11:
        return "⚪ 量能数据不足", "N/A"
    # 最后一条可能是今天（盘中），去掉后取近5/10日
    recent = hist[:-1] if len(hist) >= 12 else hist
    avg5  = sum(recent[-5:])  / 5  if len(recent) >= 5  else 0
    avg10 = sum(recent[-10:]) / 10 if len(recent) >= 10 else 0
    r5  = today_vol / avg5  if avg5  > 0 else 0
    r10 = today_vol / avg10 if avg10 > 0 else 0

    if r5 >= 2.0:
        signal = "🔴 **超级放量**"
    elif r5 >= 1.5:
        signal = "🟠 **明显放量**"
    elif r5 >= 1.2:
        signal = "🟡 放量"
    elif r5 <= 0.5:
        signal = "🟢 **明显缩量（锁筹信号）**"
    elif r5 <= 0.7:
        signal = "🟢 缩量"
    else:
        signal = "⚪ 量能正常"

    return signal, f"量比(5日均)={r5:.2f}x  量比(10日均)={r10:.2f}x"


# ──────────────────────────────────────────
# 数据源3: 东方财富 - 资金流向（海外可能受限，做好降级）
# ──────────────────────────────────────────
def get_fund_flow(code: str) -> str:
    market_id = "1" if code.startswith("6") else "0"
    url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
    params = {
        "lmt": "0", "klt": "101",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "secid": f"{market_id}.{code}",
    }
    try:
        resp = SESSION.get(url, params=params, timeout=10,
                           headers={"Referer": "https://data.eastmoney.com/"})
        data = resp.json()
        klines = data.get("data", {}).get("klines", [])
        if not klines:
            return "📊 资金流向数据暂无"
        latest = klines[-1].split(",")
        # fields2: f51=时间,f52=主力净流入,f53=小单,f54=中单,f55=大单,f56=超大单,...
        if len(latest) < 6:
            return "📊 资金流向字段不足"

        def fmt(v):
            try:
                v = float(v)
                return f"{v/1e8:.2f}亿" if abs(v) >= 1e8 else f"{v/1e4:.0f}万"
            except:
                return "N/A"

        main_net  = latest[1]  # 主力净流入
        super_net = latest[5]  # 超大单净流入
        big_net   = latest[4]  # 大单净流入
        arrow = "📈" if float(main_net) > 0 else "📉"
        return (f"{arrow} 主力净流入: **{fmt(main_net)}**\n"
                f"  超大单: {fmt(super_net)}  大单: {fmt(big_net)}")
    except Exception as e:
        return f"📊 资金流向获取失败（{type(e).__name__}）"


# ──────────────────────────────────────────
# 换手率（新浪不直接给，用成交量/流通股估算或标注N/A）
# ──────────────────────────────────────────
def fmt_money(v):
    try:
        v = float(v)
        return f"{v/1e8:.2f}亿" if v >= 1e8 else f"{v/1e4:.0f}万"
    except:
        return str(v)


def build_report(code: str, name: str, quote: dict) -> str:
    price      = quote["price"]
    change_pct = quote["change_pct"]
    change_amt = quote["change_amt"]
    high       = quote["high"]
    low        = quote["low"]
    volume     = quote["volume"]
    amount     = quote["amount"]

    if change_pct > 0:
        trend = f"🔺 +{change_pct}%  (+{change_amt})"
    elif change_pct < 0:
        trend = f"🔻 {change_pct}%  ({change_amt})"
    else:
        trend = "➡️ 平盘"

    amplitude = round((high - low) / quote["prev_close"] * 100, 2) if quote["prev_close"] else 0

    vol_signal, vol_detail = get_volume_signal(code, volume)
    fund_info = get_fund_flow(code)

    return "\n".join([
        "---",
        f"## {name}（{code}）",
        f"**现价**: {price}  {trend}",
        f"**高/低**: {high} / {low}  振幅: {amplitude}%",
        f"**成交额**: {fmt_money(amount)}  成交量: {volume:.0f}手",
        "",
        "### 量能分析",
        vol_signal,
        vol_detail,
        "",
        "### 资金流向",
        fund_info,
    ])


def main():
    print(f"[{now_cst()}] 开始获取行情数据...")

    codes = list(STOCKS.keys())
    quotes = get_realtime_quotes(codes)

    all_reports = []
    for code, name in STOCKS.items():
        print(f"  处理 {name}({code})...")
        if code not in quotes:
            all_reports.append(f"---\n## {name}（{code}）\n⚠️ 行情数据获取失败")
        else:
            all_reports.append(build_report(code, name, quotes[code]))
        time.sleep(0.5)

    advice = "\n".join([
        "---",
        "## 操作参考",
        "- 🔴 放量下跌 → 警惕出货，考虑减仓",
        "- 🟠 放量上涨 → 关注趋势延续，可持有",
        "- 🟢 缩量横盘/下跌 → 可能锁筹，观察等待",
        "- 🟢 缩量上涨 → 惜售特征，谨慎追涨",
    ])

    title = f"📊 A股监控 {now_cst()}"
    content = "\n\n".join(all_reports) + "\n\n" + advice
    send_wechat(title, content)
    print(f"[{now_cst()}] 完成")


if __name__ == "__main__":
    main()
