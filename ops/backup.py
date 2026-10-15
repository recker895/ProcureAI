"""Consistent DB + documents backup. Never expose this archive publicly."""
import sys,sqlite3,json,hashlib,zipfile,tempfile
from contextlib import closing
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import storage as db
def backup(target):
    with tempfile.TemporaryDirectory() as temp:
        snapshot=Path(temp)/'procure.db'
        # Online snapshot remains consistent even when the application is running.
        with db.connect() as source,closing(sqlite3.connect(snapshot)) as destination:source.backup(destination)
        with closing(sqlite3.connect(snapshot)) as c:
            if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Database integrity check failed')
            documents=c.execute('SELECT id,sha256 FROM documents').fetchall()
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot,'procure.db')
            for ident,digest in documents:
                path=db.DATA/'documents'/ident
                if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise RuntimeError('Document hash mismatch: '+ident)
                archive.write(path,'documents/'+ident)
            archive.writestr('manifest.json',json.dumps({'schema':1,'documents':len(documents)}))
    return target
if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('Usage: python ops/backup.py /secure/path/backup.zip')
    print(backup(sys.argv[1]))
