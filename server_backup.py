"""Administrator snapshots with validated staging and crash-recoverable replacement."""
import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import sqlite3
import time
import tempfile
import re
from contextlib import closing
from pathlib import Path
import backup
import pyzipper

MAX_ARCHIVE_BYTES=100*1024*1024
MAX_UNCOMPRESSED_BYTES=500*1024*1024
MANIFEST='manifest.json'


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


def _password(value):
    if not isinstance(value,str) or len(value)<12 or len(value)>1024: raise ValueError('Use a backup password between 12 and 1,024 characters.')
    return value.encode('utf-8')


def _files(root,currency,folder):
    names=[]
    snapshot(root/'users.sqlite3',folder/'users.sqlite3');names.append('users.sqlite3')
    with closing(connect(folder/'users.sqlite3')) as c, c:
        c.execute('DELETE FROM recovery_tokens');c.execute('DELETE FROM recovery_attempts')
        users=[dict(r) for r in c.execute('SELECT * FROM users ORDER BY id')]
    for user in users:
        source=root/financial_path(user['id'])
        if source.exists():
            name=financial_path(user['id']);snapshot(source,folder/name);names.append(name)
            with closing(connect(folder/name)) as c, c:
                c.execute('CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                c.execute("INSERT OR IGNORE INTO preferences VALUES ('currency',?)",(default_currency(root,currency),))
                c.execute('DELETE FROM previews');c.execute('DELETE FROM restore_previews')
    settings=json.loads((root/'server-settings.json').read_text()) if (root/'server-settings.json').exists() else {}
    settings.setdefault('currency',currency)
    write(folder/'server-settings.json',backup.canonical(settings));names.append('server-settings.json')
    return sorted(names)


def export(root,currency,password):
    pwd=_password(password)
    with tempfile.TemporaryDirectory(dir=root) as temp:
        folder=Path(temp);names=_files(root,currency,folder)
        if sum((folder/name).stat().st_size for name in names)>MAX_UNCOMPRESSED_BYTES: raise ValueError('Server data exceeds the 500 MB backup limit.')
        files={name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in names}
        manifest=backup.canonical({'format':'spearmint-server-backup','version':2,'encryption':'AES-256','files':files}).encode()
        out=io.BytesIO()
        with pyzipper.AESZipFile(out,'w',compression=pyzipper.ZIP_DEFLATED,encryption=pyzipper.WZ_AES) as z:
            z.setpassword(pwd);z.setencryption(pyzipper.WZ_AES,nbits=256)
            z.writestr(MANIFEST,manifest)
            for name in names:z.write(folder/name,name)
        content=out.getvalue()
    if len(content)>MAX_ARCHIVE_BYTES: raise ValueError('Encrypted server backup exceeds 100 MB. Use an offline data-folder backup.')
    return content


def _schema(c):
    return list(c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))


def _check_schema(source,target):
    with closing(connect(source)) as trusted,closing(connect(target)) as uploaded:
        if [tuple(r) for r in _schema(trusted)]!=[tuple(r) for r in _schema(uploaded)]:
            raise ValueError('The database schema is not compatible with this version of Spearmint.')


def _validate_databases(root,folder,names):
    authfile=folder/'users.sqlite3'
    _check_schema(root/'users.sqlite3',authfile)
    with closing(connect(authfile)) as c,c:
        if c.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('The user database is damaged.')
        users=[dict(r) for r in c.execute('SELECT * FROM users ORDER BY id')]
        c.execute('DELETE FROM recovery_tokens');c.execute('DELETE FROM recovery_attempts')
    if not users: raise ValueError('The backup contains no users.')
    ids={u['id'] for u in users};admins=[u for u in users if u['is_admin']]
    import auth
    for user in users:
        if type(user['id']) is not int or user['id']<1 or any(user[k] not in (0,1) for k in ('is_admin','enabled','setup_required')): raise ValueError('Invalid user record.')
        if auth.username(user['username'])!=user['username'] or not re.fullmatch('[0-9a-f]{32}:[0-9a-f]{64}',user['password_hash']): raise ValueError('Invalid user credentials.')
        if user['recovery_answers'] is not None:
            answers=json.loads(user['recovery_answers'])
            if not isinstance(answers,list) or len(answers)!=3 or not all(isinstance(a,str) and re.fullmatch('[0-9a-f]{32}:[0-9a-f]{64}',a) for a in answers): raise ValueError('Invalid recovery records.')
    if len(admins)!=1 or admins[0]['id']!=1 or not admins[0]['enabled']: raise ValueError('An enabled original administrator is required.')
    ledger_names={financial_path(i) for i in ids}
    present={n for n in names if n.endswith('.sqlite3') and n!='users.sqlite3'}
    if 'spearmint.sqlite3' not in present or not present.issubset(ledger_names): raise ValueError('Invalid user database links.')
    transactions=0
    for name in present:
        _check_schema(root/'spearmint.sqlite3',folder/name)
        with closing(connect(folder/name)) as c, c:
            c.execute('PRAGMA foreign_keys=ON')
            if c.execute('PRAGMA quick_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchone(): raise ValueError('A financial database is damaged.')
            records={t:[dict(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY 1')] for t in backup.TABLES}
            backup.validate(c,records)
            if c.execute("SELECT COUNT(*) FROM preferences WHERE key='currency'").fetchone()[0]!=1: raise ValueError('Missing profile currency.')
            transactions+=len(records['transactions'])
            c.execute('DELETE FROM previews');c.execute('DELETE FROM restore_previews')
    settings=json.loads((folder/'server-settings.json').read_text())
    code=settings.get('currency') if isinstance(settings,dict) else None
    if not isinstance(code,str) or len(code)!=3 or not code.isascii() or not code.isalpha() or not code.isupper(): raise ValueError('Invalid server currency.')
    return len(users),len(present),transactions


def preview(root,encoded,password,user_id):
    pwd=_password(password)
    if not isinstance(encoded,str): raise ValueError('Choose an encrypted Spearmint server backup.')
    if len(encoded)>4*((MAX_ARCHIVE_BYTES+2)//3): raise ValueError('Choose an encrypted backup no larger than 100 MB.')
    try: content=base64.b64decode(encoded,validate=True)
    except (ValueError,TypeError) as e: raise ValueError('The uploaded backup is invalid.') from e
    if len(content)>MAX_ARCHIVE_BYTES: raise ValueError('Choose an encrypted backup no larger than 100 MB.')
    work=root/'.server-preview'
    if work.exists(): shutil.rmtree(work)
    try:
        folder=work/'new';folder.mkdir(parents=True,mode=0o700);work.chmod(0o700)
        with pyzipper.AESZipFile(io.BytesIO(content)) as z:
            z.setpassword(pwd);infos=z.infolist()
            if sum(i.file_size for i in infos)>MAX_UNCOMPRESSED_BYTES: raise ValueError('The backup expands beyond the allowed size.')
            names=[i.filename for i in infos]
            if len(infos)>10000 or any(not (i.flag_bits&1) or getattr(i,'wz_aes_strength',None)!=3 for i in infos): raise ValueError('Every backup entry must use AES-256 encryption.')
            if any(not re.fullmatch(r'(manifest\.json|server-settings\.json|users\.sqlite3|spearmint\.sqlite3|users/[1-9][0-9]*/spearmint\.sqlite3)',n) for n in names): raise ValueError('Unexpected backup files.')
            if not {'users.sqlite3','spearmint.sqlite3','server-settings.json',MANIFEST}.issubset(names): raise ValueError('The backup is incomplete.')
            if any(i.is_dir() or '\\' in n or n.startswith('/') or '..' in Path(n).parts for i,n in zip(infos,names)): raise ValueError('The backup contains unsafe paths.')
            if len(names)!=len(set(names)) or MANIFEST not in names: raise ValueError('The backup contents are invalid.')
            manifest=json.loads(z.read(MANIFEST))
            expected=manifest.get('files') if isinstance(manifest,dict) and manifest.get('format')=='spearmint-server-backup' and manifest.get('version')==2 and manifest.get('encryption')=='AES-256' else None
            if not isinstance(expected,dict) or set(names)!={MANIFEST,*expected}: raise ValueError('The backup format is unsupported or incomplete.')
            for name,digest in expected.items():
                data=z.read(name)
                if hashlib.sha256(data).hexdigest()!=digest: raise ValueError('The backup failed its integrity check.')
                target=folder/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        users,ledgers,transactions=_validate_databases(root,folder,set(expected))
        token=secrets.token_urlsafe(24)
        write(work/'preview.json',backup.canonical({'token':token,'created':time.time(),'user_id':user_id}))
        return {'token':token,'users':users,'ledgers':ledgers,'transactions':transactions}
    except (ValueError,pyzipper.BadZipFile,RuntimeError,NotImplementedError,sqlite3.Error,OverflowError,KeyError,TypeError,UnicodeError) as e:
        shutil.rmtree(work,ignore_errors=True)
        raise ValueError('The password is incorrect or the encrypted backup is invalid. Nothing was restored.') from e


def paths(root):
    found=['users.sqlite3','spearmint.sqlite3']
    found += [str(p.relative_to(root)).replace('\\','/') for p in (root/'users').glob('*/spearmint.sqlite3') if p.parent.name.isdigit()]
    if (root/'server-settings.json').exists(): found.append('server-settings.json')
    return sorted(found)


def replace_file(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_name(target.name+'.restore-tmp')
    shutil.copyfile(source,temp)
    with open(temp,'r+b') as f: os.fsync(f.fileno())
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
