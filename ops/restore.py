"""Restore into a NEW empty destination, then verify audit and file hashes."""
import sys,zipfile,sqlite3,hashlib,os
from contextlib import closing
from pathlib import Path
def restore(source,target):
    target=Path(target).resolve()
    if target.exists() and any(target.iterdir()):raise RuntimeError('Destination must be empty; stop the application before switching data directories')
    target.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        for name in archive.namelist():
            destination=(target/name).resolve()
            if not destination.is_relative_to(target):raise RuntimeError('Unsafe archive path')
            archive.extract(name,target)
    with closing(sqlite3.connect(target/'procure.db')) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Invalid restored database')
        for ident,digest in c.execute('SELECT id,sha256 FROM documents'):
            if hashlib.sha256((target/'documents'/ident).read_bytes()).hexdigest()!=digest:raise RuntimeError('Document checksum mismatch')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]));import storage as db
    with closing(sqlite3.connect(target/'procure.db')) as c:
        c.row_factory=sqlite3.Row
        if not db.verify_audit(c):raise RuntimeError('Audit chain verification failed')
    print('Restored and verified:',target)
if __name__=='__main__':
    if len(sys.argv)!=3:raise SystemExit('Usage: python ops/restore.py backup.zip /new/empty/data')
    restore(sys.argv[1],sys.argv[2])
