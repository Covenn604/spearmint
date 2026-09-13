"""Lossless, per-user financial backups encoded as typed records in CSV."""
import csv
import hashlib
import io
import json
import secrets
import sqlite3
import time
from datetime import date

TABLES = ('accounts','categories','profiles','rules','account_profiles','transactions','preferences')
HEADER = ['record_type','data']
MAX_BYTES = 50 * 1024 * 1024
csv.field_size_limit(MAX_BYTES)


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def checksum(records):
    return hashlib.sha256(canonical(records).encode('utf-8')).hexdigest()


def export(c,currency):
    # One read transaction keeps all related tables in the same snapshot.
    c.execute('BEGIN')
    records={table:[dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY 1')] for table in TABLES}
    manifest={'format':'spearmint-financial-backup','version':2,'currency':currency,
              'counts':{t:len(records[t]) for t in TABLES},'sha256':checksum(records)}
    output=io.StringIO(newline='');writer=csv.writer(output)
    writer.writerow(HEADER);writer.writerow(['manifest',canonical(manifest)])
    for table in TABLES:
        for record in records[table]: writer.writerow([table,canonical(record)])
    text=output.getvalue()
    if len(text.encode('utf-8'))>MAX_BYTES: raise ValueError('CSV backups support up to 50 MB. Use a complete data-folder backup for larger databases.')
    return text


def parse(text,currency):
    if not isinstance(text,str) or len(text.encode('utf-8'))>MAX_BYTES:
        raise ValueError('Choose a Spearmint CSV backup no larger than 50 MB.')
    try:
        reader=csv.reader(io.StringIO(text.lstrip('\ufeff'),newline=''),strict=True)
        if next(reader)!=HEADER: raise ValueError('This is not a Spearmint backup. Use Import CSV for ordinary transaction files.')
        first=next(reader)
        if len(first)!=2 or first[0]!='manifest': raise ValueError('Backup manifest is missing.')
        manifest=json.loads(first[1]);records={t:[] for t in TABLES}
        if not isinstance(manifest,dict) or manifest.get('format')!='spearmint-financial-backup' or type(manifest.get('version')) is not int or manifest['version'] not in (1,2):
            raise ValueError('Unsupported Spearmint backup format or version.')
        if manifest.get('currency')!=currency: raise ValueError('Backup currency does not match this installation. No currency conversion is performed.')
        if manifest['version']==1: records.pop('preferences')
        for row in reader:
            if not row: continue
            if len(row)!=2 or row[0] not in records: raise ValueError('Unexpected backup record.')
            record=json.loads(row[1])
            if not isinstance(record,dict): raise ValueError('Invalid backup record.')
            records[row[0]].append(record)
        if manifest.get('counts')!={t:len(records[t]) for t in records} or manifest.get('sha256')!=checksum(records):
            raise ValueError('Backup is incomplete or changed. Restore an unmodified Spearmint backup.')
        if manifest['version']==1: records['preferences']=[{'key':'currency','value':currency}]
        if records['preferences']!=[{'key':'currency','value':currency}]: raise ValueError('Backup currency records do not match the manifest.')
        return records
    except (StopIteration,csv.Error,json.JSONDecodeError,UnicodeError,RecursionError) as error:
        raise ValueError('The backup CSV is malformed or incomplete.') from error


def populate(c,records):
    for table in reversed(TABLES): c.execute('DELETE FROM '+table)
    for table in TABLES:
        columns=list(c.execute('PRAGMA table_info('+table+')'))
        names=[r[1] for r in columns]
        for record in records[table]:
            if set(record)!=set(names): raise ValueError('Backup fields do not match the supported '+table+' format.')
            for column in columns:
                value=record[column[1]]
                if value is None:
                    if column[3] or column[5]: raise ValueError('A required backup field is empty.')
                elif ('INT' in column[2] and type(value) is not int) or ('TEXT' in column[2] and not isinstance(value,str)):
                    raise ValueError('A backup field has the wrong data type.')
            if table=='preferences' and (record['key']!='currency' or not (len(record['value'])==3 and record['value'].isascii() and record['value'].isalpha() and record['value'].isupper())):
                raise ValueError('Invalid profile preference.')
            if table=='accounts' and record['archived'] not in (0,1): raise ValueError('Invalid archived account status.')
            if table=='profiles' and not isinstance(json.loads(record['mapping']),dict): raise ValueError('Invalid saved CSV mapping.')
            if table=='transactions' and date.fromisoformat(record['date']).isoformat()!=record['date']: raise ValueError('Invalid transaction date.')
            c.execute('INSERT INTO '+table+'('+','.join(names)+') VALUES ('+','.join('?' for _ in names)+')',[record[n] for n in names])
    if c.execute('PRAGMA foreign_key_check').fetchone(): raise ValueError('The backup contains broken record links.')


def validate(c,records):
    # Build an empty staging database from trusted local schemas, never from file SQL.
    staged=sqlite3.connect(':memory:')
    try:
        staged.execute('PRAGMA foreign_keys=ON')
        for table in TABLES:
            staged.execute(c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()[0])
        for table in TABLES:
            for row in c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",(table,)):
                staged.execute(row[0])
        populate(staged,records)
    except (sqlite3.Error,OverflowError,TypeError,KeyError) as error:
        raise ValueError('The backup contains invalid or conflicting records. Nothing was restored.') from error
    finally:
        staged.close()


def preview(c,text,currency):
    records=parse(text,currency);validate(c,records)
    token=secrets.token_urlsafe(24)
    c.execute('DELETE FROM restore_previews')
    c.execute('INSERT INTO restore_previews VALUES (?,?,?)',(token,time.time(),canonical(records)))
    return {'token':token,'counts':{t:len(records[t]) for t in TABLES},'currency':currency}


def restore(c,data):
    if data.get('confirmed') is not True: raise ValueError('Confirm replacing your financial data.')
    c.execute('BEGIN IMMEDIATE')
    row=c.execute('SELECT * FROM restore_previews WHERE id=?',(data.get('token'),)).fetchone()
    if not row or row['created']<time.time()-3600: raise ValueError('Restore preview expired. Select and preview the backup again.')
    records=json.loads(row['payload'])
    validate(c,records)
    populate(c,records)
    c.execute('DELETE FROM previews')
    c.execute('DELETE FROM restore_previews')
    return {'restored':{t:len(records[t]) for t in TABLES}}
