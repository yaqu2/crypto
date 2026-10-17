"""Synthetic callbacks test software causality only; not evidence of alpha."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from prospective import ForwardStudy
from execution_path_audit import audit_run,audit_all

START=datetime(2026,10,12,14,0,tzinfo=timezone.utc)
ET=ZoneInfo('America/New_York')


def book():
    return {'bids':[{'price':round(99.99-i*.01,4),'size':1000} for i in range(5)],
            'asks':[{'price':round(100.01+i*.01,4),'size':100} for i in range(5)]}


class TestExecutionPathIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)

    def make_run(self,intervals=(0,3,15,27,39,51,63,65),quote_mode='both',fault=False,run_id='R1',closed=True):
        folder=self.base/run_id;folder.mkdir()
        study=ForwardStudy(folder/'forward_shadow',['US.TEST'],raw_run_id=run_id)
        raw=[{'runId':run_id,'kind':'run_start','origin':'local','receivedAt':START.isoformat(),
              'data':{'watchlist':{'benchmark':'US.SPY'}}}]
        for sec in intervals:
            t=START+timedelta(seconds=sec)
            for ix,code in enumerate(('US.SPY','US.TEST')):
                if quote_mode=='no_spy' and code=='US.SPY':continue
                if quote_mode=='seed' and code=='US.TEST':origin='seed'
                else:origin='push'
                r=t-timedelta(milliseconds=300-ix*100)
                st=r.astimezone(ET)
                raw.append({'runId':run_id,'kind':'quote','origin':origin,'receivedAt':r.isoformat(),
                            'callbackReceivedAt':r.isoformat(),
                            'data':[{'code':code,'data_date':st.strftime('%Y-%m-%d'),
                                     'data_time':st.strftime('%H:%M:%S'), 'last_price':100.0}]})
            b=book()
            raw.append({'runId':run_id,'kind':'book','origin':'push','receivedAt':t.isoformat(),
                        'callbackReceivedAt':t.isoformat(),
                        'data':{'code':'US.TEST','Bid':[[v['price'],v['size']] for v in b['bids']],
                                 'Ask':[[v['price'],v['size']] for v in b['asks']]}})
            study.on_book('US.TEST',b,t,allowed=True)
            if fault and sec==27:
                raw.append({'runId':run_id,'kind':'stream_reset','origin':'local',
                            'receivedAt':(t+timedelta(milliseconds=100)).isoformat(), 'data':{}})
        study.close()
        for idx,obj in enumerate(raw,1):obj['seq']=idx
        data=b''.join((json.dumps(row,ensure_ascii=False)+'\n').encode() for row in raw)
        (folder/'events.jsonl').write_bytes(data)
        meta={'eventsSha256':hashlib.sha256(data).hexdigest(),'closed':closed,
              'error':None,'dropped':0,'runId':run_id,'eventsWritten':len(raw), 'bytesWritten':len(data)}
        (folder/'metadata.json').write_text(json.dumps(meta))
        return folder/'forward_shadow'

    def test_full_causal_path_with_fresh_quotes(self):
        r=self.make_run();out=audit_run(r)
        self.assertGreater(out['modelExits'],0)
        self.assertEqual(out['pathUnverifiedExits'],0)
        self.assertEqual(out['status'],'PATH_EVIDENCE_COMPLETE_NOT_FILL_VERIFIED')
        self.assertFalse(out['brokerFillsVerified'])

    def test_long_silent_window_does_not_count_as_proven_profit(self):
        r=self.make_run((0,3,65));out=audit_run(r)
        self.assertEqual(out['modelExits'],0) # V1.6 refuses to book the fake exit
        self.assertFalse(out['brokerFillsVerified'])

    def test_fresh_spy_source_is_required_all_along_path(self):
        r=self.make_run(quote_mode='no_spy');out=audit_run(r)
        self.assertIn('BENCHMARK_QUOTE_NOT_CONTINUOUSLY_PROVEN',out['failureCounts'])

    def test_seed_quotes_cannot_prove_source_freshness(self):
        r=self.make_run(quote_mode='seed');out=audit_run(r)
        self.assertIn('TARGET_QUOTE_NOT_CONTINUOUSLY_PROVEN',out['failureCounts'])

    def test_even_an_intra_path_disconnect_causes_failure(self):
        r=self.make_run(fault=True);out=audit_run(r)
        self.assertIn('FEED_RESET_INSIDE_TRADE',out['failureCounts'])

    def test_unclosed_capture_is_not_full_evidence(self):
        r=self.make_run(closed=False)
        with self.assertRaisesRegex(ValueError,'NOT_CLOSED'):audit_run(r)

    def test_modified_raw_book_is_detected_even_with_rehashed_metadata(self):
        r=self.make_run();root=r.parent
        f=root/'events.jsonl';rows=[json.loads(x) for x in f.read_text().splitlines()]
        for row in rows:
            if row['kind']=='book':
                row['data']['Ask'][0][0]=109.0
                break
        data=b''.join((json.dumps(row)+'\n').encode() for row in rows)
        f.write_bytes(data)
        meta=json.loads((root/'metadata.json').read_text());meta['eventsSha256']=hashlib.sha256(data).hexdigest();meta['bytesWritten']=len(data)
        (root/'metadata.json').write_text(json.dumps(meta))
        with self.assertRaisesRegex(ValueError,'MISSING_OR_DIFFERENT_RAW_CALLBACK_BOOK'):audit_run(r)

    def test_whole_unverifiable_run_blocks_global_path_assurance(self):
        self.make_run(run_id='R1')
        self.make_run((0,3,65),run_id='R2')
        out=audit_all(self.base)
        self.assertEqual(out['runsFound'],2)
        self.assertFalse(out['pathEvidenceAllComplete'])
        self.assertEqual(out['runsWithVerifiedPaths'],1)

    def test_empty_directory_never_proves_anything(self):
        self.assertFalse(audit_all(self.base)['pathEvidenceAllComplete'])

if __name__=='__main__':unittest.main()

class TestQuoteClockAdversarial(unittest.TestCase):
    def test_old_provider_clock_not_refreshed_by_new_receipt(self):
        from execution_path_audit import _quote_ok
        self.assertFalse(_quote_ok([(100.0,80.0)],100.1))
        self.assertTrue(_quote_ok([(100.0,99.0)],100.1))

    def test_future_quote_receipt_cannot_repair_old_book(self):
        from execution_path_audit import _quote_ok
        self.assertFalse(_quote_ok([(101.0,100.0)],100.0))

    def test_old_valid_quote_not_extended_to_new_book(self):
        from execution_path_audit import _quote_ok
        self.assertFalse(_quote_ok([(100.0,99.0)],106.0))

    def test_alpha_gate_integrates_path_hard_veto(self):
        from alpha_gate import study_record
        with tempfile.TemporaryDirectory() as t:
            outcome=study_record(Path(t))
            self.assertIn('UNVERIFIED_INTERMEDIATE_TRADE_PATHS_OR_RAW_QUOTES',outcome['reasons'])
            self.assertFalse(outcome['executionPathEvidence']['pathEvidenceAllComplete'])
            self.assertEqual(outcome['verdict'],'NO_GO_REAL_TRADING')
