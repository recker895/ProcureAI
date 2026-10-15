"""Launch an isolated browser-test app; never point this at operational data."""
import os,sys,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
temporary=tempfile.TemporaryDirectory(prefix='procure-ui-test-');os.environ['PROCURE_DATA']=temporary.name;os.environ['PROCURE_ENV']='test';os.environ['AI_WORKER_ENABLED']='false'
import app,storage as db
db.migrate()
password=app.password_hash('isolated-browser-test-2026')
with db.transaction() as c:
    for role in ['admin','buyer','procurement','warehouse','quality','finance','viewer']:c.execute('INSERT INTO users(id,email,name,role,password) VALUES(?,?,?,?,?)',('TEST-'+role,role+'@test.local','TEST '+role,role,password))
if __name__=='__main__':
    import uvicorn
    try:uvicorn.run(app.app,host='127.0.0.1',port=8766,log_level='warning')
    finally:temporary.cleanup()
