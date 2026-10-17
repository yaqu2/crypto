#!/usr/bin/env python3
"""Read-only OpenD quote adapter, reconnect loop, authenticated loopback SSE."""
import argparse,json,threading,socket,secrets,webbrowser,fcntl
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
from core import utcnow,connection_status
from recorder import Recorder
from runtime import Runtime,validate_config
from subscription_guard import verify as verify_subscriptions
import time

ROOT=Path(__file__).resolve().parent

def collect(rt):
    try:
        from moomoo import (OpenQuoteContext,SubType,RET_OK,StockQuoteHandlerBase,OrderBookHandlerBase,
                            TickerHandlerBase,CurKlineHandlerBase,Session,KLType,AuType)
    except ImportError:
        with rt.lock:rt.state.update(connection='依赖未安装，请用Start.command启动',stage='启动失败')
        rt.error('需要官方moomoo-api依赖');return
    backoff=1
    while not rt.stop.is_set():
        ctx=None; active={'value':True}
        try:
            with rt.lock:rt.state.update(connection='正在连接本机OpenD',stage='连接中',subscriptions={})
            with socket.create_connection(('127.0.0.1',11111),timeout=3):pass
            ctx=OpenQuoteContext(host='127.0.0.1',port=11111)
            def handler(base,kind,active=active):
                class Handler(base):
                    def on_recv_rsp(self,rsp_pb):
                        ret,data=super().on_recv_rsp(rsp_pb)
                        if ret==RET_OK:
                            with rt.lock:
                                if active['value']:rt.enqueue(kind,data)
                        else:rt.error(kind+' 推送失败：'+str(data))
                        return ret,data
                return Handler()
            for base,kind in [(StockQuoteHandlerBase,'quote'),(OrderBookHandlerBase,'book'),(TickerHandlerBase,'ticker'),(CurKlineHandlerBase,'kline')]:ctx.set_handler(handler(base,kind))
            requests=[(rt.symbols,SubType.QUOTE),(rt.symbols,SubType.K_1M),(rt.config['deepSymbols'],SubType.ORDER_BOOK),(rt.config['deepSymbols'],SubType.TICKER)]
            expected=sum(len(codes) for codes,_ in requests)
            with rt.lock:rt.state['expectedSubscriptions']=expected
            for codes,kind in requests:
                if not codes:continue
                with rt.lock:rt.state['stage']='批量订阅 '+str(kind)
                opts={'session':Session.ALL} if kind in (SubType.TICKER,SubType.K_1M) else {}
                ret,data=ctx.subscribe(codes,[kind],subscribe_push=True,**opts)
                # Batch failure falls back individually so one denied symbol does not hide others.
                results=[(c,ret,data) for c in codes] if ret==RET_OK else [(c,*ctx.subscribe([c],[kind],subscribe_push=True,**opts)) for c in codes]
                for code,status,detail in results:
                    with rt.lock:rt.state['subscriptions'][code+'/'+str(kind)]='成功' if status==RET_OK else '失败'
                    rt.record('subscription',{'code':code,'subtype':str(kind),'success':status==RET_OK})
                    if status!=RET_OK:rt.error(code+' '+str(kind)+' 订阅失败：'+str(detail))
            # Seeds help display only; the evidence engine excludes them.
            ret,data=ctx.get_stock_quote(rt.symbols)
            if ret==RET_OK:rt.enqueue('quote',data,'seed')
            else:rt.error('初始报价：'+str(data))
            for code in rt.symbols:
                if rt.stop.is_set():break
                with rt.lock:rt.state['stage']='读取初始行情 '+code
                if code in rt.config['deepSymbols']:
                    ret,data=ctx.get_order_book(code,num=60)
                    if ret==RET_OK:rt.enqueue('book',data,'seed')
                if rt.state['subscriptions'].get(code+'/'+str(SubType.K_1M))=='成功':
                    ret,data=ctx.get_cur_kline(code,num=120,ktype=KLType.K_1M,autype=AuType.QFQ)
                    if ret==RET_OK:rt.enqueue('kline',data,'seed')
            # Cross-check independently via the current connection's actual subscriptions.
            check_ret,check_data=ctx.query_subscription(is_all_conn=False)
            if check_ret!=RET_OK:raise ConnectionError('无法查询OpenD真实订阅状态：'+str(check_data))
            with rt.lock:
                observed=verify_subscriptions(rt.state['subscriptions'],check_data)
                rt.state['subscriptions'].update(observed)
                rt.state['subscriptionQuota']={k:check_data.get(k) for k in ('own_used','remain','total_used')}
            if any(v!='成功' for v in observed.values()):
                rt.error('部分行情订阅未出现在OpenD当前连接的订阅清单；停止宣称全部成功')
            with rt.lock:
                rt.state['stage']='监听推送'
                rt.feed_session_start=utcnow();rt.connection_verified=True
            next_sub_check=time.monotonic()+60
            while not rt.stop.is_set():
                if time.monotonic()>=next_sub_check:
                    check_ret,check_data=ctx.query_subscription(is_all_conn=False)
                    next_sub_check=time.monotonic()+60
                    if check_ret!=RET_OK:raise ConnectionError('OpenD订阅状态查询失败：'+str(check_data))
                    with rt.lock:
                        observed=verify_subscriptions(rt.state['subscriptions'],check_data)
                        rt.state['subscriptions'].update(observed)
                        rt.state['subscriptionQuota']={k:check_data.get(k) for k in ('own_used','remain','total_used')}
                    if any(v!='成功' for v in observed.values()):
                        with rt.lock:allowed=rt.may_reconnect()
                        if allowed:raise ConnectionError('OpenD实际订阅清单缺少目标；触发限速重新订阅')
                        rt.error('订阅缺失但达到自动重连保护上限，需人工检查')
                ret,data=ctx.get_global_state()
                logged=data.get('qot_logined') if isinstance(data,dict) else None
                connected=ret==RET_OK and logged is True
                if not connected:raise ConnectionError('OpenD报价连接中断或未登录')
                backoff=1
                with rt.lock:
                    rt.connection_verified=True
                    rt.state.update(connection=connection_status(True,logged,rt.state['subscriptions'],expected=expected),heartbeatAt=utcnow())
                    quality=rt.feed_quality()
                rt.record('health',{'connected':True,'heartbeatAt':rt.state['heartbeatAt'],
                                    'feedStatus':quality['status'],'benchmarkAgeSeconds':quality['benchmarkAgeSeconds']})
                if quality['reconnectSuggested']:
                    with rt.lock:allowed=rt.may_reconnect()
                    if allowed:raise ConnectionError('基准报价长期断流；受控重连并重新订阅')
                    with rt.lock:rt.state['stage']='行情断流；自动重试达到保护上限，请检查OpenD权限和行情时段'
                rt.stop.wait(5)
        except Exception as exc:
            with rt.lock:active['value']=False
            rt.reset('连接中断；重新订阅后积累证据')
            with rt.lock:rt.state.update(connection='连接中断 · 自动重连',heartbeatAt=None,stage=f'{backoff}秒后重试')
            rt.record('health',{'connected':False,'heartbeatAt':None});rt.error(str(exc))
        finally:
            with rt.lock:active['value']=False
            if ctx is not None:
                try:ctx.close()
                except Exception:pass
        if rt.stop.wait(backoff):break
        backoff=min(30,backoff*2)

def create_server(rt,prefix):
    streams=threading.BoundedSemaphore(4)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send_common(self,ctype,length=None):
            self.send_response(200);self.send_header('Content-Type',ctype)
            if length is not None:self.send_header('Content-Length',str(length))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        def do_GET(self):
            if self.headers.get('Host') not in [f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}']:
                self.send_error(403);return
            path=urlsplit(self.path).path
            if not path.startswith(prefix):self.send_error(404);return
            tail=path[len(prefix):]
            if tail=='events':
                if not streams.acquire(blocking=False):self.send_error(503,'Too many event streams');return
                try:
                    self.connection.settimeout(2)
                    self.send_common('text/event-stream; charset=utf-8');self.end_headers()
                    revision=-1
                    while not rt.stop.is_set():
                        # Latest immutable bytes only; slow clients have no accumulating event queue.
                        with rt.lock:r,payload=rt.revision,rt.published
                        if r!=revision:
                            self.wfile.write(b'event: state\nid: '+str(r).encode()+b'\ndata: '+payload+b'\n\n');self.wfile.flush();revision=r
                        rt.stop.wait(.25)
                except (OSError,TimeoutError):pass
                finally:streams.release()
                return
            if tail=='':payload=(ROOT/'dashboard.html').read_bytes();ctype='text/html; charset=utf-8'
            elif tail=='symbol-diagnostic':
                from symbol_diagnostic import diagnose
                arg=parse_qs(urlsplit(self.path).query).get('symbol',['US.AAOI'])
                code=arg[0] if len(arg)==1 else ''
                # Never let an unbounded query create arbitrary symbols or leak account information.
                with rt.lock:
                    state=json.loads(rt.published)
                result=diagnose(state,code)
                payload=json.dumps(result,ensure_ascii=False,allow_nan=False).encode()
                ctype='application/json; charset=utf-8'
            elif tail=='news-status':
                report=ROOT/'research'/'news'/'news_status.json'
                if not report.is_file():self.send_error(404,'Configure and poll news provider first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='news-impact':
                report=ROOT/'output'/'research'/'news_impact_v28.json'
                if not report.is_file():self.send_error(404,'Run StudyNewsImpact.command first');return
                # Read-only exploratory event response; do not allow giant HTTP payloads.
                if report.stat().st_size>8_000_000:self.send_error(413,'Study report too large');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='news-research':
                report=ROOT/'research'/'news'/'news_research_report.json'
                if not report.is_file():self.send_error(404,'Run StudyNews.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='news-latest':
                from news_context import asof_news
                try:
                    result=asof_news(ROOT/'research'/'news'/'news_events.jsonl',max_age_minutes=240,top_n=20)
                    result['recentArticles']=[{k:v for k,v in x.items() if k not in ('sourceId',)} for x in result['recentArticles']]
                    payload=json.dumps(result,ensure_ascii=False,allow_nan=False).encode()
                except (ValueError,OSError) as e:
                    self.send_error(409,'News integrity failure');return
                ctype='application/json; charset=utf-8'
            elif tail=='market-candidates':
                report=ROOT/'output'/'market'/'agent_candidates.json'
                if not report.is_file():self.send_error(404,'No audited market candidate report');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='sealed-trial':
                from trial_registry import status as trial_status
                try:payload=json.dumps(trial_status(ROOT),ensure_ascii=False).encode();ctype='application/json; charset=utf-8'
                except (ValueError,OSError,KeyError) as e:
                    self.send_error(409,'Trial integrity failed: '+str(e)[:90]);return
            elif tail=='live-readiness':
                report=ROOT/'research'/'readiness'/'live_readiness.json'
                if not report.is_file():self.send_error(404,'Run Readiness.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='paper-probe':
                report=ROOT/'research'/'readiness'/'paper_probe.json'
                if not report.is_file():self.send_error(404,'Run PaperAccountCheck.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='chronological-validation':
                report=ROOT/'research'/'forward_audits'/'chronological_validation.json'
                if not report.is_file():self.send_error(404,'Run AuditChronological.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='execution-path-audit':
                report=ROOT/'research'/'forward_audits'/'execution_paths.json'
                if not report.is_file():self.send_error(404,'Run AuditExecutionPath.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='quote-pair-audit':
                report=ROOT/'research'/'latest_quote_pair_audit.json'
                if not report.is_file():self.send_error(404,'Run AuditQuotePair.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='session-coverage':
                report=ROOT/'research'/'forward_audits'/'session_coverage.json'
                if not report.is_file():self.send_error(404,'Run AuditCoverage.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='watchlist-activation':
                from activate_watchlist import audit_activations
                payload=json.dumps(audit_activations(ROOT),ensure_ascii=False).encode();ctype='application/json; charset=utf-8'
            elif tail=='raw-integrity':
                report=ROOT/'research'/'forward_audits'/'raw_integrity.json'
                if not report.is_file():self.send_error(404,'Run AuditRecorder.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='portfolio-audit':
                report=ROOT/'research'/'forward_audits'/'portfolio_stress.json'
                if not report.is_file():self.send_error(404,'No independent capital stress audit yet');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='sector-model-selection':
                report=ROOT/'output'/'research'/'sector_model_selection.json'
                if not report.is_file():self.send_error(404,'Run StudyRegimeModels.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='sector-regime-observations':
                report=ROOT/'output'/'research'/'sector_regimes.json'
                if not report.is_file():self.send_error(404,'Run StudySectorRegimes.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='order-lifecycle-rehearsal':
                report=ROOT/'output'/'rehearsals'/'order_lifecycle_report.json'
                if not report.is_file():self.send_error(404,'Run StudyOrderLifecycle.command --demo first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='market-portfolio-stress':
                report=ROOT/'output'/'market'/'market_portfolio_stress.json'
                if not report.is_file():self.send_error(404,'Run StudyPortfolioStress.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='market-robustness-report':
                report=ROOT/'output'/'market'/'market_robustness_report.json'
                if not report.is_file():self.send_error(404,'Run StudyRobustness.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='market-law-report':
                report=ROOT/'output'/'market'/'market_law_report.json'
                if not report.is_file():self.send_error(404,'Run StudyMarketLaws.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='quote-execution-stress':
                report=ROOT/'output'/'market'/'quote_execution_stress.json'
                if not report.is_file():self.send_error(404,'Run StudyExecution.command first');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='forward-panel':
                report=ROOT/'output'/'market'/'forward_panel_report.json'
                if not report.is_file():self.send_error(404,'No future scan research report');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='market':
                report=ROOT/'output'/'market'/'latest_report.json'
                if not report.is_file():self.send_error(404,'Market scan not yet run');return
                payload=report.read_bytes();ctype='application/json; charset=utf-8'
            elif tail=='recording':
                if rt.recorder is None:self.send_error(503,'Recorder unavailable');return
                try:payload=rt.recorder.export()
                except ValueError as exc:
                    self.send_error(413,str(exc));return
                except (OSError,IOError) as exc:
                    self.send_error(503,'Recording export failed integrity check');rt.error('录制导出完整性错误：'+str(exc));return
                ctype='application/zip'
            elif tail in ('state','report','snapshot'):
                payload=rt.snapshot if tail=='snapshot' else rt.published;ctype='application/json; charset=utf-8'
            else:self.send_error(404);return
            self.send_common(ctype,len(payload))
            if tail in ('snapshot','report'):self.send_header('Content-Disposition',f'attachment; filename="flowdesk-{tail}.json"')
            if tail=='recording':self.send_header('Content-Disposition',f'attachment; filename="flowdesk-recording-{rt.recorder.run_id}.zip"')
            self.end_headers()
            try:self.wfile.write(payload)
            except OSError:pass
    return ThreadingHTTPServer(('127.0.0.1',0),Handler)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--no-browser',action='store_true');parser.add_argument('--offline',action='store_true')
    parser.add_argument('--no-record',action='store_true');parser.add_argument('--config',type=Path,default=ROOT/'watchlist.json')
    args=parser.parse_args()
    config=validate_config(json.loads(args.config.read_text()))
    # A frozen manual activation cannot be bypassed by editing/replacing watchlist
    # after the operator's recorded approval. Static legacy configs remain allowed.
    if not args.offline:
        from activate_watchlist import audit_activations
        from market_archive import canonical
        import hashlib
        activation=audit_activations(ROOT)
        if activation['status']=='NO_GO_ACTIVATION_INTEGRITY':
            raise SystemExit('观察池激活记录或当前配置校验失败；拒绝启动实时研究。请检查人工修改。')
        if activation['status']=='PASS_LOCAL_ACTIVATION_LOG':
            digest=hashlib.sha256(canonical(config)).hexdigest()
            last=json.loads((ROOT/'research'/'watchlist_activation.jsonl').read_text().splitlines()[-1])
            if digest!=last['newConfigSha256']:
                raise SystemExit('实时配置不匹配已批准的全市场候选观察池；拒绝启动。')
    # Prevent two local FlowDesk collector processes from silently competing for
    # OpenD subscriptions and contaminating coverage logs. Offline checks can coexist.
    live_lock=None
    if not args.offline:
        live_lock=(ROOT/'.flowdesk_live.lock').open('a+b')
        try:fcntl.flock(live_lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            live_lock.close()
            raise SystemExit('FlowDesk 已有实时实例运行；请先关闭旧 Start.command 窗口。')
    # A frozen prospective experiment is enrolled before the first test callback.
    # Missing/broken protocol never silently becomes an independent OOS claim.
    from trial_registry import active_binding
    trial_binding={'status':'OFFLINE_NO_TRIAL'} if args.offline else active_binding(ROOT)
    if args.no_record and trial_binding.get('trialId'):
        raise SystemExit('已冻结的未来试验必须保留原始录制；禁止使用 --no-record 绕过。')
    recorder=None
    if not args.no_record:recorder=Recorder(ROOT/'recordings',symbols=config['symbols'],config=config,shadow_expected=not args.offline,app_version='1.9.0-quote-stress',trial_binding=trial_binding)

    rt=Runtime(config,recorder,forward_folder=(recorder.folder/'forward_shadow') if recorder and not args.offline else None,trial_binding=trial_binding)
    rt.state['preRegisteredTrial']=trial_binding
    if args.offline:rt.state.update(mode='offline',connection='离线检查 · 无行情连接',stage='离线')
    rt.start();rt.publish()
    prefix='/'+secrets.token_urlsafe(24)+'/'
    server=create_server(rt,prefix)
    url=f'http://127.0.0.1:{server.server_port}{prefix}'
    print('\nFlowDesk v1.9 · 全市场/多快照报价压力实验 + 数据门禁 + 只读研究。',flush=True)
    print('浏览器地址：'+url,flush=True)
    print('保持OpenD与此窗口运行。停止：Control+C。',flush=True)
    collector=None
    if not args.offline:
        collector=threading.Thread(target=collect,args=(rt,),daemon=True);collector.start()
    if not args.no_browser:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        rt.close();server.server_close()
        if recorder:recorder.close()
        if live_lock is not None:
            fcntl.flock(live_lock.fileno(),fcntl.LOCK_UN)
            live_lock.close()

if __name__=='__main__':main()
