from sqlalchemy import Column,Integer,String,JSON,Text,ForeignKey,UniqueConstraint
from src.config.database import Base

class MergeSession(Base):
    __tablename__='merge_sessions'
    id=Column(Integer,primary_key=True)
    study_id=Column(Integer,ForeignKey('studies.id'),nullable=False,index=True)
    status=Column(String(20),nullable=False,default='awaiting')
    # Common base worksheet captured when the first revision package arrives.
    base_snapshot=Column(JSON,nullable=False)
    base_checksum=Column(String(64),nullable=False)
    revision_a=Column(JSON,nullable=True)
    revision_b=Column(JSON,nullable=True)
    # Latest three-way result: rows, conflicts, decisions and merge basis.
    plan=Column(JSON,nullable=True)
    # Ordered adjudication payloads: [{conflict_id, choice, rename_key, at}]
    decisions=Column(JSON,nullable=False,default=list)
    result_checksum=Column(String(64),nullable=True)
    drift_revision=Column(Integer,nullable=False,default=0)
    note=Column(Text,nullable=False,default='')
    created_at=Column(String(40),nullable=False)
    updated_at=Column(String(40),nullable=False)

class IdempotencyRecord(Base):
    __tablename__='idempotency_records'
    __table_args__=(UniqueConstraint('idempotency_key','action'),)
    id=Column(Integer,primary_key=True)
    idempotency_key=Column(String(120),nullable=False)
    action=Column(String(40),nullable=False)
    study_id=Column(Integer,nullable=True)
    response=Column(JSON,nullable=False)
    created_at=Column(String(40),nullable=False)
