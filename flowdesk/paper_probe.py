#!/usr/bin/env python3
"""Read-only US moomoo paper-account discovery. NEVER place/modify/cancel orders.

Returns NO account identifiers, balances, trade passwords, or real-account rows.
The broker's paper venue may not supply deal-level fills or commissions;
connectivity is not a paper execution-quality test.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def _rows(data):
    if hasattr(data, 'to_dict'):
        return data.to_dict('records')
    if isinstance(data, list):
        return data
    if isinstance(data, tuple):
        return list(data)
    return []


def _enum(value):
    return str(getattr(value, 'name', value)).strip().upper().split('.')[-1]


def probe(*, context_factory=None, success_code=None, inspected_at=None):
    """The only SDK method invoked on the trading context is get_acc_list()."""
    now=inspected_at or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError('UTC_AWARE_TIME_REQUIRED')
    result={'status':'NO_GO_PAPER_CONNECTIVITY', 'inspectedAt':now.astimezone(timezone.utc).isoformat(),
            'market':'US', 'mode':'READ_ONLY_ACCOUNT_DISCOVERY', 'paperAccountCount':0,
            'paperStockAccountCount':0, 'realAccountDetailsExposed':False,
            'liveOrdersEnabled':False, 'orderApiUsed':False,
            'paperFillAndFeesAudited':False,
            'limitations':['仅验证账户发现接口，不发送模拟或实盘订单',
                           '模拟交易不代表真实成交滑点/手续费；经纪商模拟成交回报可能不完整']}
    ctx=None
    try:
        if context_factory is None:
            from moomoo import OpenSecTradeContext, TrdMarket, RET_OK
            context_factory=lambda:OpenSecTradeContext(filter_trdmarket=TrdMarket.US,
                                                       host='127.0.0.1',port=11111)
            success_code=RET_OK
        ctx=context_factory()
        status,data=ctx.get_acc_list()
        if status!=success_code:
            result['reason']='PAPER_ACCOUNT_QUERY_FAILED'
            return result
        paper=[r for r in _rows(data) if isinstance(r,dict) and _enum(r.get('trd_env'))=='SIMULATE']
        stock=[r for r in paper if _enum(r.get('sim_acc_type')) in
               ('STOCK','STOCK_AND_OPTION','STOCK_AND_OPTIONS')]
        result['paperAccountCount']=len(paper)
        result['paperStockAccountCount']=len(stock)
        if stock:
            result['status']='PAPER_ACCOUNT_DISCOVERED_NOT_EXECUTION_TESTED'
        else:
            result['reason']='NO_IDENTIFIED_US_STOCK_PAPER_ACCOUNT'
        return result
    except (ImportError,ConnectionError,OSError,RuntimeError,ValueError) as exc:
        result['reason']='OPEN_D_OR_SDK_UNAVAILABLE'
        result['errorType']=type(exc).__name__
        return result
    finally:
        if ctx is not None:
            try:ctx.close()
            except Exception:pass


def main():
    ap=argparse.ArgumentParser(description='只读检查美股 Moomoo 模拟账户；绝不下单')
    ap.add_argument('--out',type=Path,default=Path(__file__).resolve().parent/'research'/'readiness'/'paper_probe.json')
    args=ap.parse_args()
    result=probe()
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
