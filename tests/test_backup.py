import copy
import csv
import io
import json
import unittest
from unittest.mock import patch
import app
import backup
from test_app import DatabaseFixture
import test_users


class BackupTests(DatabaseFixture):
    def snapshot(self):
        with app.db() as c:
            return {t:[dict(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY 1')] for t in backup.TABLES}

    def seed(self):
        with app.db() as c:
            c.execute('UPDATE accounts SET archived=1 WHERE id=2')
            c.execute("INSERT INTO profiles VALUES ('Card mapping',?)", ('{"date":"0","nested":{"invert":true}}',))
            c.execute("INSERT INTO account_profiles VALUES (2,'Card mapping')")
            c.execute("INSERT INTO rules VALUES (42,'café',2)")
            for account,amount in ((1,-1234),(2,1234)):
                app.insert(c,dict(account_id=account,date='2026-09-13',payee='=Café, "quoted"',amount=amount,kind='transfer',category_id=None,note='Line one\r\nLine two ☕',imported_id='bank-id'),transfer_id='pair',batch_id='batch')

    def test_legacy_v1_backup_restores_with_current_currency(self):
        records=self.snapshot();records.pop('preferences')
        out=io.StringIO(newline='');w=csv.writer(out);w.writerow(backup.HEADER)
        w.writerow(['manifest',json.dumps({'format':'spearmint-financial-backup','version':1,'currency':app.CURRENCY,'counts':{t:len(r) for t,r in records.items()},'sha256':backup.checksum(records)})])
        for t,rows in records.items():
            for r in rows:w.writerow([t,json.dumps(r)])
        parsed=backup.parse(out.getvalue(),app.CURRENCY)
        self.assertEqual(parsed,self.snapshot())

    def test_exact_round_trip_and_token_replay(self):
        self.seed(); before=self.snapshot()
        with app.db() as c: text=backup.export(c,app.CURRENCY)
        self.assertEqual(backup.parse(text,app.CURRENCY),before)
        with app.db() as c:
            c.execute("UPDATE accounts SET name='Changed',opening=999 WHERE id=1")
            preview=backup.preview(c,text,app.CURRENCY)
        with app.db() as c: backup.restore(c,{'token':preview['token'],'confirmed':True})
        self.assertEqual(self.snapshot(),before)
        with self.assertRaises(ValueError):
            with app.db() as c: backup.restore(c,{'token':preview['token'],'confirmed':True})

    def test_rejects_corruption_ordinary_csv_and_currency(self):
        self.seed(); before=self.snapshot()
        with app.db() as c: text=backup.export(c,app.CURRENCY)
        for invalid in ('Date,Payee,Amount\n',text[:len(text)//2],text.replace('Café','Other'),text.replace(app.CURRENCY,'XYZ')):
            with self.assertRaises(ValueError):
                with app.db() as c: backup.preview(c,invalid,app.CURRENCY)
            self.assertEqual(self.snapshot(),before)

    def test_staging_rejects_bad_links_types_and_duplicate_ids(self):
        self.seed(); before=self.snapshot()
        invalid=[]
        for field,value in [('account_id',999),('amount','123'),('date','2026-02-30')]:
            records=copy.deepcopy(before);records['transactions'][0][field]=value;invalid.append(records)
        records=copy.deepcopy(before);records['transactions'].append(records['transactions'][0]);invalid.append(records)
        for records in invalid:
            with self.assertRaises(ValueError):
                with app.db() as c: backup.validate(c,records)
            self.assertEqual(self.snapshot(),before)

    def test_confirmation_expiry_and_failure_leave_data_untouched(self):
        self.seed();before=self.snapshot()
        with app.db() as c: text=backup.export(c,app.CURRENCY)
        with app.db() as c: p=backup.preview(c,text,app.CURRENCY)
        with self.assertRaises(ValueError):
            with app.db() as c: backup.restore(c,{'token':p['token']})
        real_populate=backup.populate
        def fail_after_writes(c,records):
            real_populate(c,records)
            if c.row_factory is not None: raise RuntimeError('simulated failure')
        with patch('backup.populate',side_effect=fail_after_writes):
            with self.assertRaises(RuntimeError):
                with app.db() as c: backup.restore(c,{'token':p['token'],'confirmed':True})
        self.assertEqual(self.snapshot(),before)
        with app.db() as c: c.execute('UPDATE restore_previews SET created=0')
        with self.assertRaises(ValueError):
            with app.db() as c: backup.restore(c,{'token':p['token'],'confirmed':True})
        self.assertEqual(self.snapshot(),before)


class BackupApiTests(unittest.TestCase):
    setUp=test_users.MultiUserTests.setUp
    tearDown=test_users.MultiUserTests.tearDown
    req=test_users.MultiUserTests.req
    login=test_users.MultiUserTests.login
    add_user=test_users.MultiUserTests.add_user

    def test_user_isolation_restore_and_credentials_preserved(self):
        self.add_user()
        status,text=self.req('/api/backup');self.assertEqual(status,200)
        self.assertNotIn('Private original',self.req('/api/backup',who='alice')[1])
        status,p=self.req('/api/backup/preview','POST',{'text':text});self.assertEqual(status,200)
        self.assertEqual(self.req('/api/backup/restore','POST',{'token':p['token'],'confirmed':True},'alice')[0],400)
        self.assertEqual(self.req('/api/backup/restore','POST',{'token':p['token']})[0],400)
        self.assertEqual(self.req('/api/backup/restore','POST',{'token':p['token'],'confirmed':True})[0],200)
        self.assertEqual(self.req('/api/backup')[1],text)
        self.assertEqual(self.req('/api/state',who='alice')[1]['accounts'],[])
        self.login('admin',test_users.PASSWORD)
        self.assertEqual(self.req('/api/backup',who='unknown')[0],401)
