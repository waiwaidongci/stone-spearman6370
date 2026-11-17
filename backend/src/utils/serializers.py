from datetime import datetime,timezone
import hashlib,json
from sqlalchemy import inspect
def now(): return datetime.now(timezone.utc).isoformat()
def record(obj): return {c.key:getattr(obj,c.key) for c in inspect(obj).mapper.column_attrs}
def _canonical(value):
    # Deep-sorted canonical form so nested dicts (e.g. measurement values) hash
    # identically regardless of insertion order. Mirrors the frontend helper.
    if isinstance(value,dict):
        return '{'+','.join(json.dumps(k,ensure_ascii=False,allow_nan=False)+':'+_canonical(value[k])
                            for k in sorted(value))+'}'
    if isinstance(value,(list,tuple)):
        return '['+','.join(_canonical(v) for v in value)+']'
    return json.dumps(value,ensure_ascii=False,allow_nan=False)
def checksum(value): return hashlib.sha256(_canonical(value).encode()).hexdigest()
