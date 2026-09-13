import json
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
        status,text=self.req('/api/server-backup');self.assertEqual(status,200)
        self.assertEqual(self.req('/api/server-backup',who='alice')[0],403)
        self.assertEqual(self.req('/api/server-backup/preview','POST',{'text':text},'alice')[0],403)
        self.add_user('bob')
        self.req('/api/accounts','POST',{'name':'Bob bank','opening':'5'},'bob')
        self.req('/api/profile','POST',{'currency':'GBP','confirmed':True},'alice')
        status,p=self.req('/api/server-backup/preview','POST',{'text':text});self.assertEqual(status,200,p)
        self.assertEqual(self.req('/api/server-backup/restore','POST',{'token':p['token']})[0],400)
        self.assertEqual(self.req('/api/server-backup/restore','POST',{'token':p['token'],'confirmation':'RESTORE SERVER'},'alice')[0],403)
        self.assertEqual(self.req('/api/server-backup/restore','POST',{'token':p['token'],'confirmation':'RESTORE SERVER'})[0],200)
        self.assertEqual(self.req('/api/state')[0],401)
        self.assertEqual(self.req('/api/state',who='alice')[0],401)
        self.login('admin',test_users.PASSWORD);self.login('alice',test_users.USER_PASSWORD)
        self.assertEqual(self.req('/api/state',who='alice')[1]['currency'],'EUR')
        self.assertEqual(self.req('/api/state',who='alice')[1]['accounts'][0]['balance'],4299)
        self.assertEqual(self.req('/api/server-backup')[1],text)
        self.assertFalse((app.DATA/'users'/'3'/'spearmint.sqlite3').exists())
        self.assertEqual(self.req('/api/server-backup/restore','POST',{'token':p['token'],'confirmation':'RESTORE SERVER'})[0],400)

    def test_corruption_invalid_links_and_expiry(self):
        text=self.req('/api/server-backup')[1]
        self.assertEqual(self.req('/api/server-backup/preview','POST',{'text':text.replace('Private original','Wrong')})[0],400)
        data=server_backup.parse(text);data['ledgers']['1']['transactions'][0]['account_id']=999
        with self.assertRaises(ValueError): server_backup.stage(app.DATA,data,app.DATA/'invalid-stage')
        p=self.req('/api/server-backup/preview','POST',{'text':text})[1]
        meta=app.DATA/'.server-preview'/'preview.json';record=json.loads(meta.read_text());record['created']=0;meta.write_text(json.dumps(record))
        self.assertEqual(self.req('/api/server-backup/restore','POST',{'token':p['token'],'confirmation':'RESTORE SERVER'})[0],400)
        self.assertEqual(self.req('/api/server-backup')[1],text)

    def test_partial_replacement_rolls_back_and_interruption_recovers(self):
        text=self.req('/api/server-backup')[1]
        self.req('/api/accounts','POST',{'name':'Keep me','opening':'5'})
        before=self.req('/api/server-backup')[1]
        for interruption in (False,True):
            p=server_backup.preview(app.DATA,text,1)
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
            self.assertEqual(self.req('/api/server-backup')[1],before)
