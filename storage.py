import sqlite3, json, hashlib, os
from pathlib import Path
from contextlib import contextmanager
from domain import now, uid
ROOT=Path(__file__).parent
DATA=Path(os.environ.get('PROCURE_DATA',ROOT/'data')).resolve()
DB=DATA/'procure.db'
class ClosingConnection(sqlite3.Connection):
    def __exit__(self,*args):
        try:return super().__exit__(*args)
        finally:self.close()
def connect():
    DATA.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(DB,timeout=15,isolation_level=None,factory=ClosingConnection);con.row_factory=sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON');con.execute('PRAGMA busy_timeout=15000')
    return con
def migrate():
    (DATA/'documents').mkdir(parents=True,exist_ok=True)
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript((ROOT/'migrations/001_initial.sql').read_text())
        c.execute('INSERT OR IGNORE INTO migrations VALUES(1,?)',(now(),))
        if not c.execute('SELECT 1 FROM migrations WHERE version=2').fetchone():
            c.executescript((ROOT/'migrations/002_automation.sql').read_text())
            c.execute('INSERT INTO migrations VALUES(2,?)',(now(),))
            audit(c,None,'migration','automation_schema_added',{'version':2,'existing_modes':'manual','legacy_external_actions':'blocked until classified'},'system')
@contextmanager
def transaction():
    c=connect();c.execute('BEGIN IMMEDIATE')
    try:yield c;c.commit()
    except BaseException:c.rollback();raise
    finally:c.close()
def dumps(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def audit(c,case_id,actor,event,detail,actor_type='user'):
    prev=c.execute('SELECT hash FROM audit ORDER BY seq DESC LIMIT 1').fetchone();previous=prev['hash'] if prev else '0'*64;created=now();detail=dumps(detail)
    digest=hashlib.sha256(dumps([case_id,actor,actor_type,event,detail,created,previous]).encode()).hexdigest()
    c.execute('INSERT INTO audit(case_id,actor,actor_type,event,detail,created,previous_hash,hash) VALUES(?,?,?,?,?,?,?,?)',(case_id,actor,actor_type,event,detail,created,previous,digest))
def save(c,data):
    data['updated']=now();raw=dumps(data)
    c.execute('INSERT INTO cases VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,data=excluded.data,updated=excluded.updated',(data['id'],data['version'],raw,data['updated']))
    c.execute('INSERT INTO versions VALUES(?,?,?,?)',(data['id'],data['version'],raw,now()))
    c.execute('INSERT OR IGNORE INTO case_controls(case_id,mode,record_kind) VALUES(?,?,?)',(data['id'],data.get('mode','manual'),data.get('record_kind','operational')))
def notify(c,case_id,role,message):
    for u in c.execute('SELECT id FROM users WHERE role=? AND active=1',(role,)).fetchall():
        c.execute('INSERT INTO notifications(id,user_id,case_id,message,created) VALUES(?,?,?,?,?)',(uid('N-'),u['id'],case_id,message,now()))
def verify_audit(c):
    previous='0'*64
    for r in c.execute('SELECT * FROM audit ORDER BY seq'):
        digest=hashlib.sha256(dumps([r['case_id'],r['actor'],r['actor_type'],r['event'],r['detail'],r['created'],previous]).encode()).hexdigest()
        if r['previous_hash']!=previous or r['hash']!=digest:return False
        previous=r['hash']
    return True
