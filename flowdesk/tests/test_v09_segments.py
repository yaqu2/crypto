"""Adversarial tests for continuous market recording and all-run provenance."""
import json,hashlib,io,tempfile,unittest,zipfile
from pathlib import Path
from recorder import Recorder
from segment_store import verify_recording
from audit_recordings import audit_all_recordings


class SegmentRecorderV09(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def make(self,**kwargs):
        r=Recorder(self.root,segment_bytes=1200,queue_size=300,**kwargs)
        for i in range(12):
            r.record('book',{'code':'US.SPY','Bid':[[500.0+i/100,1000]],
                                  'Ask':[[500.02+i/100,2000]],'tag':str(i)*40},
                     callback_received_at=f'2026-10-14T14:00:{i:02d}+00:00',callback_monotonic_ns=i)
        r.q.join();r.close()
        return r

    def test_rotation_preserves_every_record(self):
        r=self.make();v=verify_recording(r.folder)
        self.assertGreater(v['segmentCount'],2)
        self.assertEqual(v['events'],14) # start + books + stop
        self.assertEqual(v['bytes'],r.size)
        self.assertEqual(audit_all_recordings(self.root)['verdict'],'PASS_LOCAL_FILE_INTEGRITY_ONLY')

    def test_each_manifest_segment_has_independent_digest(self):
        r=self.make();m=json.loads((r.folder/'metadata.json').read_text())
        self.assertEqual(m['segments'][0]['filename'],'events.jsonl')
        for part in m['segments']:
            self.assertEqual(hashlib.sha256((r.folder/part['filename']).read_bytes()).hexdigest(),part['sha256'])

    def test_modified_second_segment_fails(self):
        r=self.make();p=r.folder/'events.000001.jsonl';p.write_bytes(p.read_bytes().replace(b'US.SPY',b'US.FAKE'))
        with self.assertRaisesRegex(ValueError,'RAW_SEGMENT_SHA256_MISMATCH'):
            verify_recording(r.folder)

    def test_deleted_middle_segment_fails(self):
        r=self.make();(r.folder/'events.000001.jsonl').unlink()
        with self.assertRaisesRegex(ValueError,'RAW_SEGMENT_MISSING_OR_UNREGISTERED'):
            verify_recording(r.folder)

    def test_extra_fake_segment_fails(self):
        r=self.make();(r.folder/'events.999999.jsonl').write_text('fake\n')
        with self.assertRaisesRegex(ValueError,'RAW_SEGMENT_MISSING_OR_UNREGISTERED'):
            verify_recording(r.folder)

    def test_truncated_line_fails(self):
        r=self.make();p=r.folder/'events.000001.jsonl';p.write_bytes(p.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError,'RAW_TRUNCATED_EVENT_LINE'):
            verify_recording(r.folder)

    def test_duplicate_row_sequence_fails_even_with_recomputed_manifest(self):
        r=self.make();p=r.folder/'events.000001.jsonl'
        raw=p.read_bytes();lines=raw.splitlines(keepends=True)
        if len(lines)<2:self.skipTest('segment too small for two rows')
        obj=json.loads(lines[1]);obj['seq']=json.loads(lines[0])['seq']
        lines[1]=(json.dumps(obj,ensure_ascii=False,separators=(',',':'))+'\n').encode();p.write_bytes(b''.join(lines))
        meta_file=r.folder/'metadata.json';meta=json.loads(meta_file.read_text())
        meta['segments'][1]['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
        meta['segments'][1]['bytes']=len(p.read_bytes())
        meta['bytesWritten']=sum(x['bytes'] for x in meta['segments'])
        meta_file.write_text(json.dumps(meta))
        with self.assertRaisesRegex(ValueError,'RAW_NONCONTIGUOUS_SEQUENCE'):
            verify_recording(r.folder)

    def test_unclosed_run_fails_closed(self):
        r=self.make();m=r.folder/'metadata.json';obj=json.loads(m.read_text());obj['closed']=False;m.write_text(json.dumps(obj))
        with self.assertRaisesRegex(ValueError,'RAW_RECORDER_NOT_CLOSED'):
            verify_recording(r.folder)

    def test_drop_is_not_clean_success(self):
        r=self.make();m=r.folder/'metadata.json';obj=json.loads(m.read_text());obj['dropped']=1;m.write_text(json.dumps(obj))
        with self.assertRaisesRegex(ValueError,'RAW_RECORDER_INTEGRITY_FAILURE'):
            verify_recording(r.folder)

    def test_full_run_cap_is_terminal(self):
        r=Recorder(self.root,max_bytes=1600,segment_bytes=800)
        for i in range(40):r.record('book',{'tag':'z'*150,'seq':i})
        r.q.join();r.close()
        self.assertGreater(r.dropped,0)
        self.assertIn('上限',r.error)
        self.assertEqual(audit_all_recordings(self.root)['verdict'],'NO_GO_RAW_INTEGRITY')

    def test_zip_prefix_contains_all_segments(self):
        r=Recorder(self.root,segment_bytes=1200)
        for i in range(12):r.record('book',{'x':i,'y':'a'*40})
        r.q.join()
        data=r.export()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            meta=json.loads(z.read('metadata.json'))
            self.assertGreater(len(meta['segments']),1)
            self.assertEqual(sum(x['events'] for x in meta['segments']),meta['exportedEvents'])
            for item in meta['segments']:
                self.assertEqual(hashlib.sha256(z.read(item['filename'])).hexdigest(),item['sha256'])
        r.close()

    def test_large_zip_rejected_without_stopping_disk_recording(self):
        r=Recorder(self.root,segment_bytes=1200,max_export_bytes=400)
        r.q.join()
        with self.assertRaisesRegex(ValueError,'EXPORT_TOO_LARGE'):
            r.export()
        self.assertFalse(r.error)
        r.close()
        self.assertEqual(verify_recording(r.folder)['status'],'VERIFIED_LOCAL_RAW_RECORDING_NOT_PROVIDER_COMPLETENESS')

    def test_one_unregistered_folder_blocks_everything(self):
        r=self.make();(self.root/'ghost').mkdir()
        x=audit_all_recordings(self.root)
        self.assertEqual(x['verdict'],'NO_GO_RAW_INTEGRITY')
        self.assertTrue(any(s['status']=='NO_GO_UNREGISTERED_RUN' for s in x['sessions']))

    def test_missing_run_blocks_everything(self):
        import shutil
        r=self.make();shutil.rmtree(r.folder)
        x=audit_all_recordings(self.root)
        self.assertEqual(x['verdict'],'NO_GO_RAW_INTEGRITY')
        self.assertEqual(x['sessions'][0]['status'],'NO_GO_MISSING_RUN_DIRECTORY')

    def test_legacy_integrity_retained(self):
        r=self.make();p=r.folder/'metadata.json';meta=json.loads(p.read_text())
        # Legacy one-segment data may still be accepted (not silently reinterpreted as multi).
        self.assertGreater(len(meta['segments']),1)
        single=self.root/'legacy';single.mkdir()
        raw=b'{"seq":1,"runId":"legacy","kind":"health"}\n'
        (single/'events.jsonl').write_bytes(raw)
        (single/'metadata.json').write_text(json.dumps({'runId':'legacy','closed':True,'error':None,
            'dropped':0,'eventsSha256':hashlib.sha256(raw).hexdigest(),
            'bytesWritten':len(raw),'eventsWritten':1}))
        self.assertEqual(verify_recording(single)['segmentCount'],1)

if __name__=='__main__':unittest.main()

class ForwardLineageAcrossSegments(unittest.TestCase):
    def test_raw_provenance_checks_book_in_later_segment(self):
        from datetime import datetime,timedelta,timezone
        from prospective import ForwardStudy
        from source_crosscheck import crosscheck
        with tempfile.TemporaryDirectory() as td:
            r=Recorder(td,segment_bytes=1800)
            lab=ForwardStudy(r.folder/'forward_shadow',['US.TEST'],raw_run_id=r.run_id)
            base=datetime(2026,10,12,14,0,tzinfo=timezone.utc)
            for t in (0,3,64):
                stamp=(base+timedelta(seconds=t)).isoformat()
                bids=[[99.99+(0.1 if t==64 else 0)-i*.01,1000] for i in range(5)]
                asks=[[100.01+(0.1 if t==64 else 0)+i*.01,100] for i in range(5)]
                raw={'code':'US.TEST','Bid':bids,'Ask':asks}
                r.record('book',raw,callback_received_at=stamp,callback_monotonic_ns=t)
                lab.on_book('US.TEST',{'bids':[{'price':x[0],'size':x[1]} for x in bids],
                                     'asks':[{'price':x[0],'size':x[1]} for x in asks]},
                            base+timedelta(seconds=t),allowed=True)
            r.q.join();lab.close();r.close()
            self.assertGreater(verify_recording(r.folder)['segmentCount'],1)
            result=crosscheck(r.folder/'forward_shadow')
            self.assertEqual(result['status'],'MATCHED_LOCAL_CALLBACK_RECORDS')
            self.assertGreater(result['paperEventsMatched'],0)
            self.assertGreater(result['rawSegmentsChecked'],1)

    def test_legacy_prefix_metadata_cannot_certify_forward_run(self):
        from datetime import datetime,timezone
        from prospective import ForwardStudy
        from source_crosscheck import crosscheck
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);run=root/'R';run.mkdir()
            study=ForwardStudy(run/'forward_shadow',['US.TEST'],raw_run_id='R')
            study.close()
            data=b'{"kind":"health","seq":1}\n'
            (run/'events.jsonl').write_bytes(data)
            (run/'metadata.json').write_text(json.dumps({'eventsSha256':hashlib.sha256(data).hexdigest(),
                         'closed':False,'exportType':'prefix snapshot; recording continues'}))
            with self.assertRaisesRegex(ValueError,'RAW_RECORDER_PREFIX_NOT_CLOSED'):
                crosscheck(run/'forward_shadow')
