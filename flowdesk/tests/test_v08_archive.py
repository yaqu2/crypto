import tempfile,unittest,json,hashlib,csv
from pathlib import Path
from market_archive import register_scan,audit_archive

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

class ArchiveV08(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        universe=b'code,name\nUS.AAA,A\nUS.BBB,B\n'
        quotes=b'code,lastPrice\nUS.AAA,99\nUS.BBB,100\n'
        (self.root/'us_stock_universe.csv').write_bytes(universe)
        (self.root/'latest_quotes.csv').write_bytes(quotes)
        report={'stockUniverseCount':2,'freshUsableCount':0,'dataGate':'NO_GO_COVERAGE',
                'universeSha256':sha(self.root/'us_stock_universe.csv'),
                'quotesSha256':sha(self.root/'latest_quotes.csv')}
        (self.root/'latest_report.json').write_text(json.dumps(report))
    def test_multiple_scans_preserved(self):
        x=register_scan(self.root);y=register_scan(self.root)
        self.assertNotEqual(x['archiveId'],y['archiveId'])
        self.assertEqual(audit_archive(self.root)['verified'],2)
        self.assertEqual(audit_archive(self.root)['status'],'PASS_LOCAL_ARCHIVE_INTEGRITY')
    def test_missing_scan_directory_fails(self):
        x=register_scan(self.root)
        import shutil
        shutil.rmtree(self.root/'archive'/x['archiveId'])
        self.assertEqual(audit_archive(self.root)['status'],'NO_GO_ARCHIVE_TAMPER')
    def test_edit_quotes_fails(self):
        x=register_scan(self.root)
        (self.root/'archive'/x['archiveId']/'latest_quotes.csv').write_text('fake')
        self.assertEqual(audit_archive(self.root)['status'],'NO_GO_ARCHIVE_TAMPER')
    def test_bad_latest_sha_cannot_archive(self):
        (self.root/'latest_quotes.csv').write_text('fake')
        with self.assertRaisesRegex(ValueError,'SNAPSHOT_SOURCE_HASH_MISMATCH'):
            register_scan(self.root)
    def test_empty_returns_no_data_not_pass(self):
        self.assertEqual(audit_archive(self.root)['status'],'NO_ARCHIVED_SCANS')

if __name__=='__main__':unittest.main()

class ArchiveOrphans(unittest.TestCase):
    def test_unregistered_archive_is_hard_error(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'archive'/'orphan').mkdir(parents=True)
            self.assertEqual(audit_archive(root)['status'],'NO_GO_UNREGISTERED_SCAN')
