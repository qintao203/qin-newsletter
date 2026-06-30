#!/usr/bin/env python3
"""秦系统 · 完整交易信号推送 — 每小时飞书推送

从OKX公开API获取数据，综合多因子分析生成可操作的做多/做空建议。
可选AI增强（设DEEPSEEK_API_KEY环境变量）。

环境变量:
  FEISHU_WEBHOOK_URL — 飞书Webhook（必设）
  DEEPSEEK_API_KEY  — DeepSeek API Key（可选，有则AI增强）
"""
import json, math, time, os, sys, urllib.request
from datetime import datetime
from typing import Optional

OKX_BASE = 'https://www.okx.com'

FEISHU_WEBHOOK = os.environ.get('FEISHU_WEBHOOK_URL', '')
DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY', '')


# ═══════════════════════════════════════════
# OKX API 调用
# ═══════════════════════════════════════════

def _okx_get(path: str) -> dict:
    url = OKX_BASE + path
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Qin-Trading-System/5.0',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {'code': '-1', 'msg': str(e)[:200]}


# ═══════════════════════════════════════════
# 技术指标计算
# ═══════════════════════════════════════════

def _ema(vals, period):
    if not vals or period <= 0:
        return vals
    k = 2.0 / (period + 1)
    r = [vals[0]]
    for v in vals[1:]:
        r.append(v * k + r[-1] * (1 - k))
    return r

def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(-period, 0)]
    gains = sum(d for d in deltas if d > 0)
    losses = sum(-d for d in deltas if d < 0)
    if losses == 0:
        return 100.0
    rs = gains / max(losses, 0.001)
    return 100.0 - 100.0 / (1.0 + rs)

def _bb_position(close, closes_20):
    """布林带位置: 0=下轨, 1=上轨"""
    if len(closes_20) < 3:
        return 0.5
    m = sum(closes_20) / len(closes_20)
    std = math.sqrt(sum((c-m)**2 for c in closes_20) / len(closes_20))
    if std == 0:
        return 0.5
    return max(0, min(1, (close - (m - 2*std)) / (4*std)))

def _ma(values, period):
    if len(values) < period:
        return values[-1] if values else 0
    return sum(values[-period:]) / period


# ═══════════════════════════════════════════
# 数据获取
# ═══════════════════════════════════════════

def get_fear_greed() -> dict:
    try:
        with urllib.request.urlopen('https://api.alternative.me/fng/?limit=1', timeout=8) as r:
            d = json.loads(r.read())
            entry = d.get('data', [{}])[0]
            return {'value': int(entry.get('value', 50)), 'classification': entry.get('value_classification', '中性')}
    except:
        return {'value': 50, 'classification': '中性'}

def get_ticker(symbol: str) -> dict:
    data = _okx_get(f'/api/v5/market/ticker?instId={symbol}')
    if data.get('code') == '0' and data.get('data'):
        t = data['data'][0]
        last = float(t.get('last', 0))
        op24 = float(t.get('open24h', 1))
        chg = (last - op24) / op24 * 100 if op24 > 0 else 0
        return {
            'price': last, 'high24h': float(t.get('high24h', 0)),
            'low24h': float(t.get('low24h', 0)),
            'change_24h': round(chg, 2), 'vol24h': float(t.get('volCcy24h', 0)),
        }
    return {'price': 0, 'change_24h': 0, 'vol24h': 0}

def get_klines(symbol: str, bar='1H', limit=100) -> list:
    data = _okx_get(f'/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}')
    result = []
    if data.get('code') == '0' and data.get('data'):
        for c in data['data']:
            result.append({
                'ts': int(c[0]), 'open': float(c[1]), 'high': float(c[2]),
                'low': float(c[3]), 'close': float(c[4]), 'volume': float(c[5]),
            })
        result.reverse()
    return result

def get_ls_ratio(ccy='BTC') -> dict:
    data = _okx_get(f'/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={ccy}')
    if data.get('code') == '0' and data.get('data'):
        ratios = data['data']
        current = float(ratios[-1][1]) if ratios else 1.0
        vals = [float(r[1]) for r in ratios[-10:]]
        avg = sum(vals)/len(vals) if vals else 1.0
        return {'ratio': round(current, 2), 'avg_10': round(avg, 2),
                'min': round(min(vals), 2), 'max': round(max(vals), 2)}
    return {'ratio': 1.0, 'avg_10': 1.0}

def get_funding_rate(symbol='BTC-USDT-SWAP') -> dict:
    data = _okx_get(f'/api/v5/public/funding-rate?instId={symbol}')
    if data.get('code') == '0' and data.get('data'):
        fr = float(data['data'][0].get('fundingRate', 0)) * 100
        return {'rate': round(fr, 4)}
    return {'rate': 0}

def get_open_interest(symbol='BTC-USDT-SWAP') -> dict:
    data = _okx_get(f'/api/v5/public/open-interest?instId={symbol}')
    if data.get('code') == '0' and data.get('data'):
        oi = data['data'][0]
        return {'oi_usd': round(float(oi.get('oiUsd', 0)), 0)}
    return {'oi_usd': 0}


# ═══════════════════════════════════════════
# 单币种深度分析
# ═══════════════════════════════════════════

def analyze_coin(symbol: str, name: str) -> dict:
    """对单个币种做多因子评分"""
    ticker = get_ticker(symbol)
    price = ticker.get('price', 0)
    if price <= 0:
        return None

    chg = ticker.get('change_24h', 0)
    vol = ticker.get('vol24h', 0)

    # 获取K线做技术分析
    klines_1h = get_klines(symbol, '1H', 50)
    if len(klines_1h) < 20:
        return None

    closes = [k['close'] for k in klines_1h]
    highs = [k['high'] for k in klines_1h]
    lows = [k['low'] for k in klines_1h]
    vols = [k['volume'] for k in klines_1h]
    last_close = closes[-1]
    last_open = klines_1h[-1]['open']

    signals = []
    score = 0  # 正=偏多, 负=偏空

    # 1. RSI信号
    rsi_val = _rsi(closes)
    if rsi_val > 70:
        signals.append(('RSI超买', -2, f'RSI{rsi_val:.0f} >70'))
        score -= 2
    elif rsi_val < 30:
        signals.append(('RSI超卖', 2, f'RSI{rsi_val:.0f} <30'))
        score += 2
    elif rsi_val > 60:
        signals.append(('RSI偏多', 1, f'RSI{rsi_val:.0f}'))
        score += 1
    elif rsi_val < 40:
        signals.append(('RSI偏空', -1, f'RSI{rsi_val:.0f}'))
        score -= 1
    else:
        signals.append(('RSI中性', 0, f'RSI{rsi_val:.0f}'))

    # 2. EMA趋势
    ema7 = _ema(closes, 7)[-1]
    ema25 = _ema(closes, 25)[-1] if len(closes) >= 25 else closes[0]
    ema99 = _ema(closes, min(99, len(closes)))[-1]
    if ema7 > ema25 > ema99:
        signals.append(('多头排列', 2, f'EMA7>{ema25:.1f}>{ema99:.1f}'))
        score += 2
    elif ema7 < ema25 < ema99:
        signals.append(('空头排列', -2, f'EMA7<{ema25:.1f}<{ema99:.1f}'))
        score -= 2
    else:
        signals.append(('均线交叉', 0, ''))

    # 3. 24h涨跌幅
    if chg > 5:
        signals.append(('暴涨过热', -1, f'+{chg:.1f}%'))
        score -= 1
    elif chg > 2:
        signals.append(('偏强', 1, f'+{chg:.1f}%'))
        score += 1
    elif chg < -5:
        signals.append(('暴跌超卖', 2, f'{chg:.1f}%'))
        score += 2
    elif chg < -2:
        signals.append(('偏弱', -1, f'{chg:.1f}%'))
        score -= 1

    # 4. 成交量异动
    avg_vol = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else sum(vols)/max(len(vols),1)
    vol_ratio = vols[-1] / max(avg_vol, 0.001)
    if vol_ratio > 2.5 and chg > 2:
        signals.append(('放量上涨', 2, f'量比{vol_ratio:.1f}x'))
        score += 2
    elif vol_ratio > 2.5 and chg < -2:
        signals.append(('放量下跌', -2, f'量比{vol_ratio:.1f}x'))
        score -= 2
    elif vol_ratio > 1.5:
        signals.append(('量能放大', 1, f'量比{vol_ratio:.1f}x'))
        score += 1 if chg > 0 else -1

    # 5. K线形态 (1H最后一根)
    body = abs(last_close - last_open)
    shadow_top = highs[-1] - max(last_close, last_open)
    shadow_bot = min(last_close, last_open) - lows[-1]
    total_range = highs[-1] - lows[-1]

    # 锤子线 (长下影+小实体)
    if total_range > 0 and shadow_bot > total_range * 0.6 and body < total_range * 0.3:
        signals.append(('锤子线(看涨)', 2, ''))
        score += 2
    # 射击之星 (长上影+小实体)
    elif total_range > 0 and shadow_top > total_range * 0.6 and body < total_range * 0.3:
        signals.append(('射击之星(看跌)', -2, ''))
        score -= 2
    # 光头阳线 (强势)
    elif last_close > last_open and shadow_top < total_range * 0.1:
        signals.append(('光头阳线', 1, ''))
        score += 1
    # 光脚阴线 (弱势)
    elif last_close < last_open and shadow_bot < total_range * 0.1:
        signals.append(('光脚阴线', -1, ''))
        score -= 1

    # 6. 多空比 (如果有数据)
    ccy = name
    ls = get_ls_ratio(ccy)
    ls_r = ls.get('ratio', 1.0)
    if ls_r >= 2.5:
        signals.append(('多空过热', -2, f'多空比{ls_r}'))
        score -= 2
    elif ls_r <= 0.5:
        signals.append(('多空极冷', 2, f'多空比{ls_r}'))
        score += 2
    elif ls_r >= 2.0:
        signals.append(('多头拥挤', -1, f'多空比{ls_r}'))
        score -= 1
    elif ls_r <= 0.8:
        signals.append(('空头主导', 1, f'多空比{ls_r}'))
        score += 1

    # 7. 资金费率 (BTC近似)
    fr_res = get_funding_rate(symbol)
    fr = fr_res.get('rate', 0)
    if fr < -0.05:
        signals.append(('轧空信号', 2, f'费率{fr:.4f}%'))
        score += 2
    elif fr > 0.05:
        signals.append(('多头过热', -2, f'费率{fr:.4f}%'))
        score -= 2
    elif fr < -0.01:
        signals.append(('空头付钱', 1, f'费率{fr:.4f}%'))
        score += 1
    elif fr > 0.01:
        signals.append(('多头付钱', -1, f'费率{fr:.4f}%'))
        score -= 1

    # 综合判断
    if score >= 5:
        direction = '🟢强烈做多'
        confidence = min(90, 50 + score * 5)
    elif score >= 2:
        direction = '🟢做多'
        confidence = 50 + score * 5
    elif score <= -5:
        direction = '🔴强烈做空'
        confidence = min(90, 50 + abs(score) * 5)
    elif score <= -2:
        direction = '🔴做空'
        confidence = 50 + abs(score) * 5
    else:
        direction = '⚪观望'
        confidence = 50

    return {
        'symbol': name, 'price': price, 'change_24h': chg,
        'score': score, 'direction': direction, 'confidence': confidence,
        'signals': signals, 'rsi': round(rsi_val, 1),
        'vol_ratio': round(vol_ratio, 1),
        'ls_ratio': ls_r, 'funding_rate': fr,
    }


# ═══════════════════════════════════════════
# AI增强分析 (可选)
# ═══════════════════════════════════════════

def ai_analysis(market_data: dict) -> Optional[str]:
    """调用DeepSeek API做AI分析"""
    if not DEEPSEEK_KEY:
        return None

    prompt = f"""你是秦交易系统首席AI分析师。基于以下市场数据，给出当前最值得交易的币种推荐。

当前市场:
- BTC: ${market_data['btc_price']:.2f} (24h {market_data['btc_chg']:+.2f}%)
- 恐慌贪婪: {market_data['fear_greed_value']} ({market_data['fear_greed_label']})
- BTC多空比: {market_data['btc_ls']}
- 评分Top 3做多: {', '.join(m['symbol'] for m in market_data['top_longs'][:3]) if market_data['top_longs'] else '无'}
- 评分Top 3做空: {', '.join(m['symbol'] for m in market_data['top_shorts'][:3]) if market_data['top_shorts'] else '无'}

请给出:
1. 当前市场整体判断 (一句话)
2. 推荐做多的2-3个币 + 理由 + 入场参考价
3. 推荐做空的2-3个币 + 理由 + 入场参考价
4. 风险提示

简短精炼，实战交易员口吻，中文。"""

    try:
        body = json.dumps({
            'model': 'deepseek-chat',
            'messages': [
                {'role': 'system', 'content': '你是秦交易系统AI分析师，专注合约交易信号，回答简洁有力。'},
                {'role': 'user', 'content': prompt},
            ],
            'max_tokens': 600,
            'temperature': 0.7,
        }).encode()
        req = urllib.request.Request(
            'https://api.deepseek.com/v1/chat/completions',
            data=body,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {DEEPSEEK_KEY}',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result.get('choices', [{}])[0].get('message', {}).get('content', '')
    except Exception as e:
        return f'[AI分析暂不可用: {str(e)[:80]}]'


# ═══════════════════════════════════════════
# 主分析流程
# ═══════════════════════════════════════════

COINS = [
    ('BTC-USDT-SWAP', 'BTC'), ('ETH-USDT-SWAP', 'ETH'), ('SOL-USDT-SWAP', 'SOL'),
    ('DOGE-USDT-SWAP', 'DOGE'), ('XRP-USDT-SWAP', 'XRP'), ('UNI-USDT-SWAP', 'UNI'),
    ('ATOM-USDT-SWAP', 'ATOM'), ('LINK-USDT-SWAP', 'LINK'), ('AVAX-USDT-SWAP', 'AVAX'),
    ('ARB-USDT-SWAP', 'ARB'), ('OP-USDT-SWAP', 'OP'), ('NEAR-USDT-SWAP', 'NEAR'),
    ('AAVE-USDT-SWAP', 'AAVE'), ('CRV-USDT-SWAP', 'CRV'), ('GRASS-USDT-SWAP', 'GRASS'),
    ('ACT-USDT-SWAP', 'ACT'), ('AI-USDT-SWAP', 'AI'), ('ENA-USDT-SWAP', 'ENA'),
    ('ZRO-USDT-SWAP', 'ZRO'), ('METIS-USDT-SWAP', 'METIS'), ('W-USDT-SWAP', 'W'),
    ('APT-USDT-SWAP', 'APT'), ('SUI-USDT-SWAP', 'SUI'), ('SEI-USDT-SWAP', 'SEI'),
    ('TIA-USDT-SWAP', 'TIA'), ('PENDLE-USDT-SWAP', 'PENDLE'), ('ONDO-USDT-SWAP', 'ONDO'),
    ('JUP-USDT-SWAP', 'JUP'), ('WIF-USDT-SWAP', 'WIF'), ('PEPE-USDT-SWAP', 'PEPE'),
]

def run_analysis() -> dict:
    """执行全市场综合分析"""
    # 基础市场数据
    fg = get_fear_greed()
    btc_ticker = get_ticker('BTC-USDT-SWAP')
    btc_ls = get_ls_ratio('BTC')
    btc_oi = get_open_interest('BTC-USDT-SWAP')
    btc_fr = get_funding_rate('BTC-USDT-SWAP')

    # 分析每个币
    results = []
    for sym, name in COINS:
        try:
            r = analyze_coin(sym, name)
            if r:
                results.append(r)
        except:
            continue

    # 排序
    longs = sorted([r for r in results if r['score'] >= 2], key=lambda x: x['score'], reverse=True)
    shorts = sorted([r for r in results if r['score'] <= -2], key=lambda x: x['score'])
    neutrals = [r for r in results if -2 < r['score'] < 2]

    market_data = {
        'btc_price': btc_ticker.get('price', 0),
        'btc_chg': btc_ticker.get('change_24h', 0),
        'fear_greed_value': fg.get('value', 50),
        'fear_greed_label': fg.get('classification', '中性'),
        'btc_ls': btc_ls.get('ratio', 1.0),
        'btc_ls_signal': '过热' if btc_ls.get('ratio', 1.0) >= 2.5 else ('偏冷' if btc_ls.get('ratio', 1.0) <= 0.8 else '中性'),
        'top_longs': longs[:5],
        'top_shorts': shorts[:5],
    }

    # AI分析
    ai_text = ai_analysis(market_data)

    return {
        'fg': fg, 'btc': btc_ticker, 'btc_ls': btc_ls,
        'btc_oi': btc_oi, 'btc_fr': btc_fr,
        'longs': longs, 'shorts': shorts, 'neutrals': neutrals,
        'total_analyzed': len(results),
        'ai_analysis': ai_text,
        'timestamp': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
    }


# ═══════════════════════════════════════════
# 飞书消息格式化
# ═══════════════════════════════════════════

def format_feishu(rpt: dict) -> dict:
    lines = []
    lines.append('📡 **秦系统 · 小时级信号推送**')
    lines.append('')

    # 市场概览
    fg = rpt.get('fg', {})
    btc = rpt.get('btc', {})
    ls = rpt.get('btc_ls', {})
    oi = rpt.get('btc_oi', {})
    fr = rpt.get('btc_fr', {})
    lines.append(f'**₿ BTC**: ${btc.get("price",0):,.2f} (24h {btc.get("change_24h",0):+.2f}%)')
    lines.append(f'**😱 恐慌**: {fg.get("value","?")} ({fg.get("classification","?")})')
    lines.append(f'**📊 多空比**: {ls.get("ratio",1.0):.2f} | **💰 费率**: {fr.get("rate",0):.4f}%')
    lines.append(f'**💼 持仓**: ${oi.get("oi_usd",0):,.0f}')
    lines.append('')

    longs = rpt.get('longs', [])
    shorts = rpt.get('shorts', [])
    neutrals = rpt.get('neutrals', [])
    total = rpt.get('total_analyzed', 0)

    # 做多推荐
    if longs:
        lines.append(f'**🟢 做多信号 ({len(longs)})**')
        for c in longs[:5]:
            s = c['score']
            stars = '⭐' * min(3, max(1, s // 3))
            lines.append(f'{stars} {c["symbol"]}: ${c["price"]:.4f} (24h {c["change_24h"]:+.2f}%) | 置信度{c["confidence"]}% | RSI{c["rsi"]}')
            # 关键信号摘要
            key_sigs = [f'{sig[2] or sig[0]}' for sig in c['signals'][:3] if abs(sig[1]) >= 1]
            if key_sigs:
                lines.append(f'   → {", ".join(key_sigs)}')
        lines.append('')

    # 做空推荐
    if shorts:
        lines.append(f'**🔴 做空信号 ({len(shorts)})**')
        for c in shorts[:5]:
            s = abs(c['score'])
            stars = '⭐' * min(3, max(1, s // 3))
            lines.append(f'{stars} {c["symbol"]}: ${c["price"]:.4f} (24h {c["change_24h"]:+.2f}%) | 置信度{c["confidence"]}% | RSI{c["rsi"]}')
            key_sigs = [f'{sig[2] or sig[0]}' for sig in c['signals'][:3] if abs(sig[1]) >= 1]
            if key_sigs:
                lines.append(f'   → {", ".join(key_sigs)}')
        lines.append('')

    # AI分析
    ai_text = rpt.get('ai_analysis')
    if ai_text:
        lines.append(f'**🧠 AI分析**')
        lines.append(ai_text[:600])
        lines.append('')

    # 统计
    lines.append(f'---')
    lines.append(f'扫描{total}个币种 | 做多{len(longs)} 做空{len(shorts)} 观望{len(neutrals)}')
    lines.append(f'⏱ {rpt.get("timestamp","")}')
    lines.append('🤖 秦系统自动推送 | 每小时更新')

    return {'msg_type': 'text', 'content': {'text': '\n'.join(lines)}}


def send_feishu(msg: dict) -> bool:
    if not FEISHU_WEBHOOK:
        print('❌ FEISHU_WEBHOOK_URL 未设置')
        return False
    try:
        data = json.dumps(msg).encode()
        req = urllib.request.Request(FEISHU_WEBHOOK, data=data,
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            ok = result.get('code') == 0
            print(f'{"✅" if ok else "❌"} 飞书推送: {result}')
            return ok
    except Exception as e:
        print(f'❌ 飞书推送异常: {e}')
        return False


# ═══════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════

if __name__ == '__main__':
    start = time.time()
    print(f'📡 秦系统 · 小时级信号推送 ({datetime.now().strftime("%Y-%m-%d %H:%M")})')
    print(f'   扫描 {len(COINS)} 个币种...')

    report = run_analysis()
    elapsed = time.time() - start

    longs = report.get('longs', [])
    shorts = report.get('shorts', [])
    neutrals = report.get('neutrals', [])

    print(f'\n✅ 分析完成 ({elapsed:.1f}s)')
    print(f'   BTC: ${report["btc"]["price"]:,.2f} (24h {report["btc"]["change_24h"]:+.2f}%)')
    print(f'   恐慌贪婪: {report["fg"]["value"]} ({report["fg"]["classification"]})')
    print(f'   做多信号: {len(longs)}个 | 做空信号: {len(shorts)}个 | 观望: {len(neutrals)}个')

    if longs:
        print(f'\n🟢 做多推荐:')
        for c in longs[:5]:
            print(f'   {c["symbol"]:6s} | 评分{c["score"]:+d} | 置信度{c["confidence"]}% | ${c["price"]:<10.4f} | 24h {c["change_24h"]:+.2f}%')
    if shorts:
        print(f'\n🔴 做空推荐:')
        for c in shorts[:5]:
            print(f'   {c["symbol"]:6s} | 评分{c["score"]:+d} | 置信度{c["confidence"]}% | ${c["price"]:<10.4f} | 24h {c["change_24h"]:+.2f}%')

    if report.get('ai_analysis'):
        print(f'\n🧠 AI分析:\n{report["ai_analysis"][:300]}')

    # 飞书推送
    msg = format_feishu(report)
    send_feishu(msg)

    print(f'\n⏱ 总耗时: {time.time()-start:.1f}s')
