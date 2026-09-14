import json
import base64
import io
import pyzipper
import hashlib
import tempfile
from pathlib import Path
from contextlib import closing
PASSWORD="separate-backup-password-123"
import unittest
from unittest.mock import patch
import app
import backup
import server_backup
import test_users

class ServerBackupTests(unittest.TestCase):
    setUp=test_users.MultiUserTests.setUp
    tearDown=test_users.MultiUserTests.tearDown
    req=test_users.MultiUserTests.req
    login=test_users.MultiUserTests.login
    add_user=test_users.MultiUserTests.add_user

    def archive(self):
        with app.SERVER_LOCK:
            return base64.b64encode(server_backup.export(app.DATA,app.CURRENCY,PASSWORD)).decode()

    def records(self):
        with app.db() as c:return backup.export(c,app.CURRENCY)

    def repack(self,files,encrypted=True):
        out=io.BytesIO()
        with pyzipper.AESZipFile(out,'w',compression=pyzipper.ZIP_DEFLATED,
                                 encryption=pyzipper.WZ_AES if encrypted else None) as z:
            if encrypted:z.setpassword(PASSWORD.encode());z.setencryption(pyzipper.WZ_AES,nbits=256)
            for name,data in files.items():z.writestr(name,data)
        return base64.b64encode(out.getvalue()).decode()

    def archive_files(self):
        with pyzipper.AESZipFile(io.BytesIO(base64.b64decode(self.archive()))) as z:
            z.setpassword(PASSWORD.encode())
            return {n:z.read(n) for n in z.namelist()}

    def assert_rejected(self,encoded):
        before=self.records()
        self.assertEqual(self.req('/api/server-backup/preview','POST',{'archive':encoded,'password':PASSWORD})[0],400)
        self.assertEqual(self.records(),before)
        self.assertFalse((app.DATA/'.server-preview').exists())

    def test_archive_encryption_and_wal_snapshot_settings_preserved(self):
        server_backup.write(app.DATA/'server-settings.json',json.dumps({'currency':'CAD','extra_setting':'preserved'}))
        with closing(server_backup.connect(app.DATA/'spearmint.sqlite3')) as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute("UPDATE transactions SET note='Committed in WAL'");c.commit()
            encoded=self.archive()
        with pyzipper.AESZipFile(io.BytesIO(base64.b64decode(encoded))) as z:
            self.assertTrue(all(i.flag_bits&1 and i.wz_aes_strength==3 for i in z.infolist()))
            with self.assertRaises(RuntimeError):z.read('users.sqlite3')
            z.setpassword(PASSWORD.encode())
            self.assertTrue(z.read('users.sqlite3').startswith(b'SQLite format 3'))
            self.assertEqual(json.loads(z.read('server-settings.json'))['extra_setting'],'preserved')
        p=self.req('/api/server-backup/preview','POST',{'archive':encoded,'password':PASSWORD})[1]
        with closing(server_backup.connect(app.DATA/'.server-preview/new/spearmint.sqlite3')) as c:
            self.assertEqual(c.execute('SELECT note FROM transactions').fetchone()[0],'Committed in WAL')
        self.assertNotIn(PASSWORD,(app.DATA/'.server-preview/preview.json').read_text())

    def test_rejects_corruption_unencrypted_traversal_limits_and_schema(self):
        files=self.archive_files()
        self.assert_rejected(self.repack(files,False))
        self.assert_rejected(self.repack({**files,'../escaped':b'bad'}))
        self.assert_rejected(self.repack({**files,'spearmint.sqlite3':b'corrupt'}))
        encoded=self.repack(files)
        with patch('server_backup.MAX_UNCOMPRESSED_BYTES',1):self.assert_rejected(encoded)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'test.sqlite3';path.write_bytes(files['spearmint.sqlite3'])
            with closing(server_backup.connect(path)) as c:
                c.execute("CREATE TRIGGER hostile AFTER INSERT ON transactions BEGIN DELETE FROM accounts; END");c.commit()
            files['spearmint.sqlite3']=path.read_bytes()
        manifest=json.loads(files['manifest.json'])
        manifest['files']['spearmint.sqlite3']=hashlib.sha256(files['spearmint.sqlite3']).hexdigest()
        files['manifest.json']=json.dumps(manifest).encode()
        self.assert_rejected(self.repack(files))

    def test_download_endpoint_password_confirmation_and_binary_response(self):
        import http.client
        self.assertEqual(self.req('/api/server-backup','POST',{'password':PASSWORD,'confirmation':'mismatch'})[0],400)
        self.assertEqual(self.req('/api/server-backup','POST',{'password':'short','confirmation':'short'})[0],400)
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        c.request('POST','/api/server-backup',json.dumps({'password':PASSWORD,'confirmation':PASSWORD}),
                  {'X-Requested-With':'MonthlySpend','Cookie':self.cookies['admin']})
        response=c.getresponse();body=response.read();c.close()
        self.assertEqual(response.status,200);self.assertEqual(response.getheader('Content-Type'),'application/zip')
        self.assertTrue(body.startswith(b'PK'))
        self.assertEqual(self.req('/api/server-backup')[0],404)

    def test_currency_is_personal_and_preserves_amounts(self):
        self.add_user()
        self.assertEqual(self.req('/api/profile','POST',{'currency':'EUR','confirmed':True},'alice')[0],200)
        self.assertEqual(self.req('/api/state',who='alice')[1]['currency'],'EUR')
        self.assertEqual(self.req('/api/state')[1]['currency'],app.CURRENCY)
        self.assertEqual(self.req('/api/profile','POST',{'currency':'XYZ','confirmed':True})[0],400)
        self.assertEqual(self.req('/api/profile','POST',{'currency':'USD'})[0],400)
        self.assertEqual(self.req('/api/profile','POST',{'currency':'USD','confirmed':True})[0],200)
        self.assertEqual(self.req('/api/state')[1]['accounts'][0]['balance'],12000)
        status,text=self.req('/api/backup');self.assertEqual(status,200)
        self.assertEqual(backup.parse(text,'USD')['preferences'],[{'key':'currency','value':'USD'}])

    def test_complete_round_trip_removes_new_users_and_invalidates_sessions(self):
        self.add_user()
        self.req('/api/profile','POST',{'currency':'EUR','confirmed':True},'alice')
        self.req('/api/accounts','POST',{'name':'Alice bank','opening':'42.99'},'alice')
        text=self.archive();before=self.records()
        self.assertEqual(self.req('/api/server-backup',who='alice')[0],403)
        self.assertEqual(self.req('/api/server-backup/preview','POST',{'archive':text,'password':PASSWORD},'alice')[0],403)
        self.add_user('bob')
        self.req('/api/accounts','POST',{'name':'Bob bank','opening':'5'},'bob')
        self.req('/api/profile','POST',{'currency':'GBP','confirmed':True},'alice')
        status,p=self.req('/api/server-backup/preview','POST',{'archive':text,'password':PASSWORD});self.assertEqual(status,200,p)
        self.assertEqual(self.req('/api/server-backup/restore','POST',{'token':p['token']})[0],400)
        self.assertEqual(self.req('/api/server-backup/restore','POST',{'token':p['token'],'confirmation':'RESTORE SERVER'},'alice')[0],403)
        self.assertEqual(self.req('/api/server-backup/restore','POST',{'token':p['token'],'confirmation':'RESTORE SERVER'})[0],200)
        self.assertEqual(self.req('/api/state')[0],401)
        self.assertEqual(self.req('/api/state',who='alice')[0],401)
        self.login('admin',test_users.PASSWORD);self.login('alice',test_users.USER_PASSWORD)
        self.assertEqual(self.req('/api/state',who='alice')[1]['currency'],'EUR')
        self.assertEqual(self.req('/api/state',who='alice')[1]['accounts'][0]['balance'],4299)
        self.assertEqual(self.records(),before)
        self.assertFalse((app.DATA/'users'/'3'/'spearmint.sqlite3').exists())
        self.assertEqual(self.req('/api/server-backup/restore','POST',{'token':p['token'],'confirmation':'RESTORE SERVER'})[0],400)

    def test_corruption_invalid_links_and_expiry(self):
        text=self.archive();before=self.records()
        self.assertEqual(self.req('/api/server-backup/preview','POST',{'archive':text,'password':'wrong-password-123'})[0],400)
        self.assertFalse((app.DATA/'.server-preview').exists())
        p=self.req('/api/server-backup/preview','POST',{'archive':text,'password':PASSWORD})[1]
        meta=app.DATA/'.server-preview'/'preview.json';record=json.loads(meta.read_text());record['created']=0;meta.write_text(json.dumps(record))
        self.assertEqual(self.req('/api/server-backup/restore','POST',{'token':p['token'],'confirmation':'RESTORE SERVER'})[0],400)
        self.assertEqual(self.records(),before)

    def test_partial_replacement_rolls_back_and_interruption_recovers(self):
        text=self.archive();before=self.records()
        self.req('/api/accounts','POST',{'name':'Keep me','opening':'5'})
        before=self.records()
        for interruption in (False,True):
            p=server_backup.preview(app.DATA,text,PASSWORD,1)
            real=server_backup.replace_file;calls=0
            def fail(source,target):
                nonlocal calls
                calls+=1
                if calls==2:
                    if interruption: raise KeyboardInterrupt('simulated process interruption')
                    raise OSError('simulated disk error')
                return real(source,target)
            with patch('server_backup.replace_file',side_effect=fail):
                with self.assertRaises(KeyboardInterrupt if interruption else OSError):
                    server_backup.restore(app.DATA,{'token':p['token'],'confirmation':'RESTORE SERVER'},1)
            server_backup.recover(app.DATA)
            self.assertEqual(self.records(),before)
