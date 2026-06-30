#!/usr/bin/env python3
"""秦系统 · 量化消息面日报 — 独立版

可在任何环境运行（无需本地代理/无需Hermes/无需Termux）。
使用OKX公开API + 飞书Webhook推送。

环境变量:
  FEISHU_WEBHOOK_URL — 飞书机器人Webhook地址
  (不设则只输出到stdout)
"""
import json
import time
import urllib.request
import os
import sys
from datetime import datetime


# ====== OKX公开API端点 (无需API Key) ======
OKX_BASE = 'https://www.okx.com'

# ====== 飞书Webhook ======
FEISHU_WEBHOOK = os.environ.get('FEISHU_WEBHOOK_URL', '')
# 后备: 硬编码的webhook (仅供GitHub Actions secret未设时兜底)
FALLBACK_WEBHOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/0e01b4c8-4059-49f7-848d-c143c03b98e4'
if not FEISHU_WEBHOOK:
    FEISHU_WEBHOOK = FALLBACK_WEBHOOK


def _okx_get(path: str) -> dict:
    """调用OKX公开API"""
    url = OKX_BASE + path
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Qin-Newsletter-Bot/1.0',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {'code': '-1', 'msg': str(e)[:200]}


def get_fear_greed() -> dict:
    """恐慌贪婪指数"""
    try:
        with urllib.request.urlopen('https://api.alternative.me/fng/?limit=1', timeout=8) as r:
            data = json.loads(r.read())
            entry = data.get('data', [{}])[0]
            return {
                'value': int(entry.get('value', 50)),
                'classification': entry.get('value_classification', '中性'),
            }
    except:
        return {'value': 50, 'classification': '中性'}


def get_btc_ticker() -> dict:
    """BTC实时行情"""
    data = _okx_get('/api/v5/market/ticker?instId=BTC-USDT-SWAP')
    if data.get('code') == '0' and data.get('data'):
        t = data['data'][0]
        last = float(t.get('last', 0))
        op24 = float(t.get('open24h', 1))
        chg = (last - op24) / op24 * 100 if op24 > 0 else 0
        return {
            'price': last,
            'high24h': float(t.get('high24h', 0)),
            'low24h': float(t.get('low24h', 0)),
            'change_24h': round(chg, 2),
            'vol24h': float(t.get('volCcy24h', 0)),
        }
    return {'price': 0, 'change_24h': 0}


def get_ls_ratio() -> dict:
    """BTC多空比"""
    data = _okx_get('/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC')
    if data.get('code') == '0' and data.get('data'):
        ratios = data['data']
        current = float(ratios[-1][1]) if ratios else 1.0
        vals = [float(r[1]) for r in ratios[-20:]]
        avg = sum(vals) / len(vals) if vals else 1.0
        return {'current': round(current, 2), 'avg_20': round(avg, 2)}
    return {'current': 1.0, 'avg_20': 1.0}


def get_open_interest() -> dict:
    """BTC持仓量"""
    data = _okx_get('/api/v5/public/open-interest?instId=BTC-USDT-SWAP')
    if data.get('code') == '0' and data.get('data'):
        oi = data['data'][0]
        oi_usd = float(oi.get('oiUsd', 0))
        return {'oi_usd': oi_usd}
    return {'oi_usd': 0}


def get_top_movers(top_n: int = 10) -> list:
    """扫描全市场找异动币种"""
    data = _okx_get(f'/api/v5/market/tickers?instType=SWAP&limit={top_n + 20}')
    movers = []
    if data.get('code') == '0' and data.get('data'):
        for t in data['data'][:top_n + 10]:
            sym = t.get('instId', '').split('-')[0]
            last = float(t.get('last', 0))
            op24 = float(t.get('open24h', 1))
            if op24 <= 0:
                continue
            chg = (last - op24) / op24 * 100
            vol = float(t.get('volCcy24h', 0))
            if abs(chg) >= 3 and vol > 50000:
                movers.append({
                    'symbol': sym,
                    'change': round(chg, 2),
                    'price': last,
                    'vol': round(vol, 0),
                })
    movers.sort(key=lambda x: abs(x['change']), reverse=True)
    return movers[:top_n]


def get_funding_rate() -> dict:
    """BTC资金费率"""
    data = _okx_get('/api/v5/public/funding-rate?instId=BTC-USDT-SWAP')
    if data.get('code') == '0' and data.get('data'):
        fr = float(data['data'][0].get('fundingRate', 0)) * 100
        return {'funding_rate': round(fr, 4)}
    return {'funding_rate': 0}


def generate_report() -> dict:
    """生成完整消息面报告"""
    fg = get_fear_greed()
    btc = get_btc_ticker()
    ls = get_ls_ratio()
    oi = get_open_interest()
    fr = get_funding_rate()
    movers = get_top_movers(8)

    # 综合评分
    scores = []
    fg_v = fg.get('value', 50)
    if fg_v <= 20:
        scores.append(('恐慌贪婪', 20))
    elif fg_v <= 40:
        scores.append(('恐慌贪婪', 60))
    else:
        scores.append(('恐慌贪婪', 50))

    ls_r = ls.get('current', 1.0)
    if ls_r >= 2.5:
        scores.append(('多空比', 15))
    elif ls_r <= 0.8:
        scores.append(('多空比', 80))
    else:
        scores.append(('多空比', 50))

    weighted = sum(s[1] for s in scores) / max(len(scores), 1)
    if weighted >= 70:
        overall = '🟢偏多'
    elif weighted <= 35:
        overall = '🔴偏空'
    else:
        overall = '🟡中性'

    return {
        'overall': overall,
        'score': round(weighted, 0),
        'fear_greed': fg,
        'btc': btc,
        'long_short': ls,
        'open_interest': oi,
        'funding_rate': fr,
        'movers': movers,
        'timestamp': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
    }


def format_feishu_message(report: dict) -> dict:
    """格式化为飞书消息JSON"""
    btc = report.get('btc', {})
    fg = report.get('fear_greed', {})
    ls = report.get('long_short', {})
    oi = report.get('open_interest', {})
    fr = report.get('funding_rate', {})
    movers = report.get('movers', [])

    price_str = f"${btc.get('price', 0):,.2f}" if btc.get('price') else 'N/A'
    chg_str = f"{btc.get('change_24h', 0):+.2f}%" if btc.get('change_24h') is not None else 'N/A'

    content = (
        f"📡 **秦系统 · 量化消息面日报**\n"
        f"综合研判: {report.get('overall', '?')} (评分: {report.get('score', 50)})\n\n"
        f"---\n"
        f"**😱 恐慌贪婪**: {fg.get('value', '?')} ({fg.get('classification', '?')})\n"
        f"**₿ BTC**: {price_str} (24h {chg_str})\n"
        f"**📊 多空比**: {ls.get('current', 1.0)}\n"
        f"**💼 持仓量**: ${oi.get('oi_usd', 0):,.0f}\n"
        f"**💰 资金费率**: {fr.get('funding_rate', 0):.4f}%\n"
    )

    if movers:
        content += f"\n**⚡ 异动币 ({len(movers)}个)**\n"
        for m in movers[:6]:
            d = '📈' if m['change'] > 0 else '📉'
            content += f"{d} {m['symbol']}: {m['change']:+.2f}%\n"

    content += f"\n⏱ {report.get('timestamp', '')}\n"
    content += "🤖 秦系统自动生成 | 数据源: OKX"

    return {
        'msg_type': 'text',
        'content': {'text': content},
    }


def send_to_feishu(message: dict) -> bool:
    """发送到飞书"""
    if not FEISHU_WEBHOOK:
        print('❌ 未设FEISHU_WEBHOOK_URL，跳过飞书推送')
        return False

    try:
        data = json.dumps(message).encode('utf-8')
        req = urllib.request.Request(
            FEISHU_WEBHOOK,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get('code') == 0:
                print(f'✅ 飞书推送成功')
                return True
            else:
                print(f'❌ 飞书推送失败: {result}')
                return False
    except Exception as e:
        print(f'❌ 飞书推送异常: {e}')
        return False


# ====== 主入口 ======
if __name__ == '__main__':
    import time
    start = time.time()

    print('📡 秦系统 · 量化消息面报告生成中...')
    report = generate_report()
    elapsed = time.time() - start

    # 终端输出
    btc = report.get('btc', {})
    fg = report.get('fear_greed', {})
    ls = report.get('long_short', {})
    print(f'\n✅ 报告已生成 ({elapsed:.1f}s)')
    print(f'   综合研判: {report.get("overall", "?")} (评分: {report.get("score", 50)})')
    print(f'   BTC: ${btc.get("price", 0):,.2f} (24h {btc.get("change_24h", 0):+.2f}%)')
    print(f'   恐慌贪婪: {fg.get("value", "?")} ({fg.get("classification", "?")})')
    print(f'   多空比: {ls.get("current", 1.0)}')
    print(f'   异动币: {len(report.get("movers", []))}个')

    # 推送飞书
    message = format_feishu_message(report)
    send_to_feishu(message)
    print(f'\n⏱ 总耗时: {time.time()-start:.1f}s')
