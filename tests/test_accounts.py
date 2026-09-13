import sqlite3
import unittest
import app
import test_app
import test_users


class AccountTests(test_app.DatabaseFixture):
    def transfer(self,c,destination=2):
        a=app.insert(c,dict(account_id=1,date='2026-09-01',payee='Move',amount=-100,kind='transfer',category_id=None,note='',transfer_id='pair'))
        b=app.insert(c,dict(account_id=destination,date='2026-09-01',payee='Move',amount=100,kind='transfer',category_id=None,note='',transfer_id='pair'))
        return a,b
    def test_edit_preserves_transactions_and_legacy_opening_only_updates(self):
        with app.db() as c:
            key=self.tx(c,-300)
            before=dict(c.execute('SELECT * FROM transactions').fetchone())
            app.edit_account(c,1,{'name':'Renamed bank','opening':'-237.84'})
            self.assertEqual(c.execute('SELECT name,opening FROM accounts WHERE id=1').fetchone()[:],('Renamed bank',-23784))
            self.assertEqual(dict(c.execute('SELECT * FROM transactions').fetchone()),before)
            app.edit_account(c,1,{'opening':'10'})
            self.assertEqual(c.execute('SELECT name FROM accounts WHERE id=1').fetchone()[0],'Renamed bank')
            with self.assertRaises(app.Invalid): app.edit_account(c,1,{'name':''})
            with self.assertRaises(sqlite3.IntegrityError): app.edit_account(c,1,{'name':'Visa'})
    def test_keep_archives_without_changing_any_transactions(self):
        with app.db() as c:
            self.transfer(c);self.tx(c,-300)
            c.execute("INSERT INTO profiles VALUES ('Bank','{}')")
            app.set_profile_default(c,1,'Bank')
            before=[dict(r) for r in c.execute('SELECT * FROM transactions ORDER BY id')]
        with app.db() as c: app.delete_account(c,1,{'transactions':'keep','confirmed':True})
        with app.db() as c:
            self.assertEqual([dict(r) for r in c.execute('SELECT * FROM transactions ORDER BY id')],before)
            self.assertEqual(c.execute('SELECT archived FROM accounts WHERE id=1').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM account_profiles').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM profiles').fetchone()[0],1)
            for fn in [lambda: app.active_account(c,1),lambda: app.preview(c,{'account_id':1}),lambda: app.set_profile_default(c,1,'Bank')]:
                with self.assertRaises(app.Invalid): fn()
            data={'account_id':1,'date':'2026-09-02','payee':'Edited','amount':'3','kind':'expense'}
            with self.assertRaises(app.Invalid): app.transaction(c,data)
            self.assertEqual(app.transaction(c,data,original_account=1)['account_id'],1)
    def test_move_preserves_records_and_opening_unlinks_only_merged_pair(self):
        with app.db() as c:
            self.transfer(c);key=self.tx(c,-300)
            c.execute("INSERT INTO accounts(id,name) VALUES (3,'Other')")
            orphan=app.insert(c,dict(account_id=3,date='2026-09-01',payee='Existing unpaired',amount=-50,kind='transfer',category_id=None,note='',transfer_id='unrelated'))
            before=[dict(r) for r in c.execute('SELECT * FROM transactions WHERE account_id=1 ORDER BY id')]
        with app.db() as c: app.delete_account(c,1,{'transactions':'move','destination_id':2,'confirmed':True})
        with app.db() as c:
            self.assertIsNone(c.execute('SELECT 1 FROM accounts WHERE id=1').fetchone())
            self.assertEqual(c.execute('SELECT opening FROM accounts WHERE id=2').fetchone()[0],-5000)
            for old in before:
                new=dict(c.execute('SELECT * FROM transactions WHERE id=?',(old['id'],)).fetchone())
                self.assertEqual(new,{**old,'account_id':2,'transfer_id':None})
            self.assertEqual(c.execute('SELECT transfer_id FROM transactions WHERE id=?',(orphan,)).fetchone()[0],'unrelated')
    def test_remove_keeps_counterpart_amount_and_kind(self):
        with app.db() as c:
            a,b=self.transfer(c);self.tx(c,-300)
            before=dict(c.execute('SELECT * FROM transactions WHERE id=?',(b,)).fetchone())
        with app.db() as c: app.delete_account(c,1,{'transactions':'remove','confirmed':True})
        with app.db() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM transactions')]
            self.assertEqual(rows,[{**before,'transfer_id':None}])
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
    def test_invalid_delete_and_import_id_conflict_roll_back(self):
        with app.db() as c:
            self.tx(c,-300)
            c.execute("UPDATE transactions SET imported_id='same'")
            app.insert(c,dict(account_id=2,date='2026-09-01',payee='Other',amount=-100,kind='expense',category_id=None,note='',imported_id='same'))
        for data in [{},{'transactions':'remove'},{'transactions':'bad','confirmed':True},{'transactions':'move','destination_id':1,'confirmed':True},{'transactions':'move','destination_id':2,'confirmed':True},{'transactions':'move','destination_id':999,'confirmed':True}]:
            with self.assertRaises(app.Invalid):
                with app.db() as c: app.delete_account(c,1,data)
            with app.db() as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],2)
                self.assertEqual(c.execute('SELECT archived FROM accounts WHERE id=1').fetchone()[0],0)
    def test_migration_retains_existing_accounts_and_activity(self):
        with app.db() as c:
            self.tx(c,-300)
            c.execute('ALTER TABLE accounts DROP COLUMN archived')
        app.init();app.init()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT name,opening,archived FROM accounts WHERE id=1').fetchone()[:],('Chequing',10000,0))
            self.assertEqual(c.execute('SELECT amount FROM transactions').fetchone()[0],-300)


class AccountApiTests(unittest.TestCase):
    setUp=test_users.MultiUserTests.setUp
    tearDown=test_users.MultiUserTests.tearDown
    req=test_users.MultiUserTests.req
    login=test_users.MultiUserTests.login
    add_user=test_users.MultiUserTests.add_user
    def test_archived_account_history_visible_and_other_user_isolated(self):
        self.add_user()
        self.assertEqual(self.req('/api/accounts/1','DELETE',{'transactions':'remove','confirmed':True},'alice')[0],400)
        self.assertEqual(self.req('/api/accounts/1','PUT',{'name':'Renamed bank','opening':'100'})[0],200)
        self.assertEqual(self.req('/api/accounts/1','DELETE',{'transactions':'keep','confirmed':True})[0],200)
        state=self.req('/api/state')[1]
        self.assertEqual(state['accounts'],[])
        self.assertEqual(state['archived_accounts'][0]['name'],'Renamed bank')
        self.assertEqual(self.req('/api/transactions')[1]['transactions'][0]['account'],'Renamed bank')
        self.assertEqual(self.req('/api/state',who='alice')[1]['archived_accounts'],[])
