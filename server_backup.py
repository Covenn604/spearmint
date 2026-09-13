"""Administrator snapshots with validated staging and crash-recoverable replacement."""
import csv
import io
import json
import os
import secrets
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
import backup


def default_currency(root,fallback):
    p=root/'server-settings.json'
    return json.loads(p.read_text())['currency'] if p.exists() else fallback


def financial_path(key):
    return 'spearmint.sqlite3' if key==1 else f'users/{key}/spearmint.sqlite3'


def connect(path):
    c=sqlite3.connect(path);c.row_factory=sqlite3.Row
    return c


def snapshot(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    with closing(connect(source)) as src,closing(connect(target)) as dst:
        src.backup(dst)
        dst.execute('PRAGMA journal_mode=DELETE')


def write(path,content):
    temp=path.with_name(path.name+'.tmp')
    with open(temp,'w',encoding='utf-8') as f:
        f.write(content);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)


def export(root,currency):
    with closing(connect(root/'users.sqlite3')) as c:
        users=[dict(r) for r in c.execute('SELECT * FROM users ORDER BY id')]
        seq=c.execute("SELECT seq FROM sqlite_sequence WHERE name='users'").fetchone()
    ledgers={}
    for user in users:
        path=root/financial_path(user['id'])
        if path.exists():
            with closing(connect(path)) as c:
                c.execute('CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                c.execute("INSERT OR IGNORE INTO preferences VALUES ('currency',?)",(default_currency(root,currency),));c.commit()
                c.execute('BEGIN')
                ledgers[str(user['id'])]={t:[dict(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY 1')] for t in backup.TABLES}
    data={'users':users,'sequence':seq[0] if seq else 1,'ledgers':ledgers,'currency':default_currency(root,currency)}
    out=io.StringIO(newline='');w=csv.writer(out);w.writerow(backup.HEADER)
    w.writerow(['manifest',backup.canonical({'format':'spearmint-server-backup','version':1,'sha256':backup.checksum(data)})])
    w.writerow(['server',backup.canonical(data)])
    text=out.getvalue()
    if len(text.encode())>backup.MAX_BYTES: raise ValueError('Server CSV exceeds 50 MB. Use an offline data-folder backup.')
    return text


def parse(text):
    if not isinstance(text,str) or len(text.encode())>backup.MAX_BYTES: raise ValueError('Choose a server CSV backup no larger than 50 MB.')
    try:
        rows=list(csv.reader(io.StringIO(text.lstrip('\ufeff'),newline=''),strict=True))
        if len(rows)!=3 or rows[0]!=backup.HEADER or len(rows[1])!=2 or len(rows[2])!=2 or rows[1][0]!='manifest' or rows[2][0]!='server': raise ValueError('Choose an unmodified Spearmint server backup.')
        manifest=json.loads(rows[1][1]);data=json.loads(rows[2][1])
        if manifest.get('format')!='spearmint-server-backup' or manifest.get('version')!=1 or manifest.get('sha256')!=backup.checksum(data): raise ValueError('Server backup is unsupported, incomplete, or changed.')
        if set(data)!={'users','sequence','ledgers','currency'} or not isinstance(data['users'],list) or not isinstance(data['ledgers'],dict): raise ValueError('Invalid server records.')
        code=data['currency']
        if not isinstance(code,str) or len(code)!=3 or not code.isascii() or not code.isalpha() or not code.isupper(): raise ValueError('Invalid server currency.')
        return data
    except (csv.Error,KeyError,TypeError,AttributeError,RecursionError) as e:
        raise ValueError('Invalid server backup.') from e


def stage(root,data,folder):
    folder.mkdir(parents=True)
    authfile=folder/'users.sqlite3';snapshot(root/'users.sqlite3',authfile)
    with closing(connect(authfile)) as c, c:
        columns=list(c.execute('PRAGMA table_info(users)'));names=[r['name'] for r in columns]
        c.execute('DELETE FROM users');c.execute('DELETE FROM recovery_tokens');c.execute('DELETE FROM recovery_attempts')
        ids=set()
        for user in data['users']:
            if not isinstance(user,dict) or set(user)!=set(names): raise ValueError('Unsupported user record fields.')
            for col in columns:
                v=user[col['name']]
                if v is None:
                    if col['notnull'] or col['pk']: raise ValueError('Missing user field.')
                elif ('INT' in col['type'] and type(v) is not int) or ('TEXT' in col['type'] and not isinstance(v,str)): raise ValueError('Invalid user field type.')
            if user['id']<1 or any(user[k] not in (0,1) for k in ('is_admin','enabled','setup_required')): raise ValueError('Invalid user status.')
            import auth
            if auth.username(user['username'])!=user['username']: raise ValueError('Invalid username.')
            import re
            if not re.fullmatch('[0-9a-f]{32}:[0-9a-f]{64}',user['password_hash']): raise ValueError('Invalid password record.')
            if user['recovery_answers'] is not None:
                answers=json.loads(user['recovery_answers'])
                if not isinstance(answers,list) or len(answers)!=3 or not all(isinstance(a,str) and re.fullmatch('[0-9a-f]{32}:[0-9a-f]{64}',a) for a in answers): raise ValueError('Invalid recovery records.')
            c.execute('INSERT INTO users('+','.join(names)+') VALUES ('+','.join('?' for n in names)+')',[user[n] for n in names]);ids.add(user['id'])
        admins=[u for u in data['users'] if u['is_admin']]
        if len(admins)!=1 or admins[0]['id']!=1 or not admins[0]['enabled']: raise ValueError('An enabled original administrator is required.')
        if type(data['sequence']) is not int or data['sequence']<max(ids): raise ValueError('Invalid user sequence.')
        c.execute("UPDATE sqlite_sequence SET seq=? WHERE name='users'",(data['sequence'],))
    if '1' not in data['ledgers'] or not set(data['ledgers']).issubset({str(i) for i in ids}): raise ValueError('Invalid user database links.')
    for key,records in data['ledgers'].items():
        if set(records)!=set(backup.TABLES): raise ValueError('Unsupported financial tables.')
        target=folder/financial_path(int(key));snapshot(root/'spearmint.sqlite3',target)
        with closing(connect(target)) as c, c:
            c.execute('PRAGMA foreign_keys=ON');backup.validate(c,records);backup.populate(c,records)
            if c.execute("SELECT COUNT(*) FROM preferences WHERE key='currency'").fetchone()[0]!=1: raise ValueError('Missing profile currency.')
            c.execute('DELETE FROM previews');c.execute('DELETE FROM restore_previews')
    write(folder/'server-settings.json',backup.canonical({'currency':data['currency']}))


def preview(root,text,user_id):
    data=parse(text);work=root/'.server-preview'
    if work.exists(): shutil.rmtree(work)
    try:
        stage(root,data,work/'new')
        token=secrets.token_urlsafe(24)
        write(work/'preview.json',backup.canonical({'token':token,'created':time.time(),'user_id':user_id}))
        return {'token':token,'users':len(data['users']),'ledgers':len(data['ledgers']),'transactions':sum(len(r['transactions']) for r in data['ledgers'].values())}
    except (sqlite3.Error,OverflowError,KeyError,TypeError) as e:
        shutil.rmtree(work,ignore_errors=True)
        raise ValueError('Invalid or conflicting server records. Nothing was restored.') from e


def paths(root):
    found=['users.sqlite3','spearmint.sqlite3']
    found += [str(p.relative_to(root)).replace('\\','/') for p in (root/'users').glob('*/spearmint.sqlite3') if p.parent.name.isdigit()]
    if (root/'server-settings.json').exists(): found.append('server-settings.json')
    return sorted(found)


def replace_file(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_name(target.name+'.restore-tmp')
    shutil.copyfile(source,temp)
    with open(temp,'rb') as f: os.fsync(f.fileno())
    os.replace(temp,target)


def recover(root):
    work=root/'.server-restore';journal=work/'pending.json'
    if not journal.exists(): return
    record=json.loads(journal.read_text())
    for name in record['targets']:
        target=root/name
        for suffix in ('-wal','-shm','-journal'): Path(str(target)+suffix).unlink(missing_ok=True)
        if name in record['originals']: replace_file(work/'old'/name,target)
        else: target.unlink(missing_ok=True)
    journal.unlink();shutil.rmtree(work)


def restore(root,data,user_id):
    if data.get('confirmation')!='RESTORE SERVER': raise ValueError('Type RESTORE SERVER to confirm replacing all users and data.')
    preview=root/'.server-preview';meta=preview/'preview.json'
    if not meta.exists(): raise ValueError('Validate a server backup first.')
    record=json.loads(meta.read_text())
    if record['token']!=data.get('token') or record['user_id']!=user_id or record['created']<time.time()-3600: raise ValueError('Server restore preview expired.')
    work=root/'.server-restore'
    if work.exists(): shutil.rmtree(work)
    work.mkdir();old=work/'old';old.mkdir()
    originals=paths(root);new=paths(preview/'new');targets=sorted(set(originals+new))
    # Requests hold the server lock. Checkpoint all live files before replacement.
    for name in originals:
        source=root/name;target=old/name
        if name.endswith('.sqlite3'):
            with closing(connect(source)) as c: c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            snapshot(source,target)
        else: target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    write(work/'pending.json',backup.canonical({'targets':targets,'originals':originals}))
    try:
        for name in targets:
            target=root/name
            for suffix in ('-wal','-shm','-journal'): Path(str(target)+suffix).unlink(missing_ok=True)
            if name in new: replace_file(preview/'new'/name,target)
            else: target.unlink(missing_ok=True)
        (work/'pending.json').unlink() # Commit point. Earlier interruptions roll back on startup.
    except Exception:
        recover(root);raise
    shutil.rmtree(work,ignore_errors=True);shutil.rmtree(preview,ignore_errors=True)
