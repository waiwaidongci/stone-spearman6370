"""Persistence orchestration for the two-laboratory three-way merge.

Guarantees implemented here:
* the formal worksheet is never touched while conflicts are unresolved;
* every write is idempotent under the supplied idempotency key;
* all study-mutating transactions take a row lock on the study first, so
  concurrent package intake, adjudication and ordinary edits serialize;
* drift of the formal worksheet during adjudication forces a recompute, and
  prior decisions are reused only while their three-way fingerprint survives;
* the final worksheet replacement is one atomic transaction, after which new
  research runs may freeze the new inputs.
"""
from fastapi import HTTPException
from sqlalchemy import select
from src.models.study import Study
from src.models.observation import Observation
from src.models.merge import MergeSession,IdempotencyRecord
from src.constants.merge import MERGE_STATUS,SIDE_LABELS
from src.services import studies
from src.services.merge_engine import compute,apply_decisions,package_checksum,_validate_decision
from src.services.domain_validation import validate_values
from src.services.audits import log
from src.utils.serializers import record,now,checksum

def lock_study(db,study_id):
    # MySQL/InnoDB honours FOR UPDATE; SQLite raises on the clause and falls
    # back to its single-writer serialization, which is enough for local tests.
    try:
        study=db.scalar(select(Study).where(Study.id==study_id).with_for_update())
    except Exception:
        study=db.get(Study,study_id)
    if not study: raise HTTPException(404,'业务批次不存在')
    return study

def worksheet_snapshot(db,study_id):
    rows=studies.observations(db,study_id)
    payload=[{'id':r['id'],'source_key':r['source_key'],'values':r['values']} for r in rows]
    return payload,checksum(payload)

def _idempotent(db,key,action):
    return db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.idempotency_key==key,IdempotencyRecord.action==action))

def _store_idempotent(db,key,action,study_id,response):
    db.add(IdempotencyRecord(idempotency_key=key,action=action,study_id=study_id,
                             response=response,created_at=now()))

def _validate_package(data):
    if len(data.rows)>5000: raise HTTPException(422,'小型研究最多5000行')
    ids=[];refs=[];clean=[]
    for row in data.rows:
        if row.id is None and not row.client_ref:
            raise HTTPException(422,'新增行必须携带 client_ref，沿用底稿行必须携带 id')
        if row.id is not None and row.client_ref:
            raise HTTPException(422,'同一行不能同时携带 id 与 client_ref')
        try: values=validate_values(row.values)
        except (ValueError,TypeError) as e: raise HTTPException(422,str(e))
        item={'source_key':row.source_key,'values':values}
        if row.id is not None: item['id']=row.id;ids.append(row.id)
        else: item['client_ref']=row.client_ref;refs.append(row.client_ref)
        clean.append(item)
    if len(ids)!=len(set(ids)) or len(refs)!=len(set(refs)):
        raise HTTPException(422,'修订包内身份（id/client_ref）重复，无法稳定跟踪改名')
    # Intra-package key repeats are allowed: renumbering onto a live key becomes
    # a key_occupation conflict; final uniqueness is enforced again at apply.
    return clean

def receive_package(db,study_id,data,user):
    action='merge.package'
    lock_study(db,study_id)
    existing=_idempotent(db,data.idempotency_key,action)
    if existing:
        db.refresh(existing)
        if existing.study_id!=study_id: raise HTTPException(409,'幂等键已用于其他批次')
        log(db,user,'merge.package.replay',f'{study_id}/{data.lab_name}')
        db.commit()
        return existing.response
    base_rows,base_sum=worksheet_snapshot(db,study_id)
    if not base_rows: raise HTTPException(422,'该批次尚无共同底稿，无法离线合并')
    if data.base_checksum!=base_sum:
        raise HTTPException(409,f'共同底稿已变化（当前SHA256 {base_sum[:16]}…），请重新导出底稿后再修订')
    clean=_validate_package(data)
    base_ids={r['id'] for r in base_rows}
    unknown=sorted({r['id'] for r in clean if 'id' in r}-base_ids)
    if unknown: raise HTTPException(422,'修订包含有底稿不存在的行 id：'+', '.join(map(str,unknown[:10])))
    pkg={'lab_name':data.lab_name,'author':data.author,'revision':data.revision,
         'note':data.note,'rows':clean,'checksum':package_checksum(clean),
         'received_at':now()}
    session=_open_session(db,study_id,base_rows,base_sum,data,pkg,user)
    response=session_view(session)
    _store_idempotent(db,data.idempotency_key,action,study_id,response)
    log(db,user,'merge.package',f'{study_id}/{data.lab_name}/rev{data.revision}/rows{len(clean)}')
    db.commit()
    return response

def _open_session(db,study_id,base_rows,base_sum,data,pkg,user):
    prior=db.scalar(select(MergeSession).where(MergeSession.study_id==study_id,
        MergeSession.status.in_(MERGE_STATUS[:3])).order_by(MergeSession.id.desc()))
    if prior and prior.base_checksum!=base_sum:
        prior.status='superseded';prior.updated_at=now()
        prior=None
    if prior is None:
        if data.revision!=1: raise HTTPException(422,'该批次尚无进行中的合并会话，修订号必须从1开始')
        side=_side_slot(db,study_id,data.lab_name)
        s=MergeSession(study_id=study_id,status='awaiting',base_snapshot=base_rows,
                       base_checksum=base_sum,decisions=[],drift_revision=0,
                       note=f'{SIDE_LABELS[side]}发起的离线合并',
                       created_at=now(),updated_at=now())
        setattr(s,f'revision_{side.lower()}',pkg)
        db.add(s);db.flush()
        return s
    side=_side_for_lab(db,prior,data.lab_name)
    slot=f'revision_{side.lower()}'
    stored=getattr(prior,slot)
    if stored and (stored.get('lab_name')!=data.lab_name or stored.get('author')!=data.author):
        raise HTTPException(409,f'{SIDE_LABELS[side]}席位已由 {stored.get("lab_name")}/{stored.get("author")} 占用')
    if stored and stored['checksum']==pkg['checksum'] and stored['revision']==data.revision:
        return prior  # identical resend before idempotency layer (defensive)
    if stored and data.revision<=stored['revision']:
        raise HTTPException(409,f'已收到更新的修订 rev{stored["revision"]}，旧修订不能覆盖')
    setattr(prior,slot,pkg);prior.updated_at=now()
    if prior.revision_a and prior.revision_b:
        _recompute(db,prior)
    db.flush()
    return prior

def _side_slot(db,study_id,lab_name):
    # A returning laboratory keeps the slot it used in any earlier session;
    # new laboratories take the first free slot of the current active session.
    for s in db.scalars(select(MergeSession).where(
            MergeSession.study_id==study_id).order_by(MergeSession.id)):
        for side,slot in (('A','revision_a'),('B','revision_b')):
            pkg=getattr(s,slot)
            if pkg and pkg.get('lab_name')==lab_name: return side
    active=db.scalar(select(MergeSession).where(MergeSession.study_id==study_id,
        MergeSession.status.in_(MERGE_STATUS[:3])).order_by(MergeSession.id.desc()))
    if active:
        if not active.revision_a: return 'A'
        if not active.revision_b: return 'B'
    return 'A'

def _side_for_lab(db,session,lab_name):
    for side,slot in (('A','revision_a'),('B','revision_b')):
        pkg=getattr(session,slot)
        if pkg and pkg.get('lab_name')==lab_name: return side
    return _side_slot(db,session.study_id,lab_name)

def _recompute(db,session):
    base_rows=session.base_snapshot
    base_rows_now,base_sum_now=worksheet_snapshot(db,session.study_id)
    if base_sum_now==session.base_checksum:
        plan=compute(base_rows,session.revision_a,session.revision_b,session.decisions)
    else:
        # Formal worksheet drifted: recompute against the new base while the
        # packages keep referencing the original common worksheet.
        session.drift_revision+=1
        plan=compute(base_rows_now,session.revision_a,session.revision_b,
                     session.decisions,original_base_rows=base_rows)
        session.base_snapshot=base_rows_now;session.base_checksum=base_sum_now
    session.plan=plan;session.decisions=plan['decisions']
    session.status='conflicts' if plan['conflicts'] else 'resolved'
    session.updated_at=now()

def get_session(db,session_id):
    s=db.get(MergeSession,session_id)
    if not s: raise HTTPException(404,'合并会话不存在')
    return s

def adjudicate(db,session_id,data,user):
    action='merge.decide'
    s=get_session(db,session_id)
    lock_study(db,s.study_id)
    if s.status=='applied': raise HTTPException(409,'该合并已整批落地，不能再裁决')
    if s.status=='superseded': raise HTTPException(409,'该会话已被新会话取代')
    key=data.idempotency_key or f'decide-{session_id}-'+checksum([d.model_dump() for d in data.decisions])
    existing=_idempotent(db,key,action)
    if existing:
        db.commit();return existing.response
    # Drift check: formal worksheet changed while adjudication was in progress.
    _,base_sum=worksheet_snapshot(db,s.study_id)
    if base_sum!=s.base_checksum or not s.plan:
        before={d['conflict_id']:d.get('fingerprint') for d in (s.decisions or [])}
        _recompute(db,s)
        after={d['conflict_id']:d.get('fingerprint') for d in s.decisions}
        log(db,user,'merge.recompute',f'{session_id}/drift{s.drift_revision}/kept{len(after)}/lost{len(before)-len(after)}')
    if s.status=='awaiting': raise HTTPException(409,'双方修订包尚未到齐')
    plan=s.plan
    by_cid={c['conflict_id']:c for c in plan['conflicts']}
    known=set(by_cid)
    incoming=[d.model_dump(exclude_none=True) for d in data.decisions]
    for item in incoming:
        if item['conflict_id'] not in known: raise HTTPException(409,f"冲突 {item['conflict_id']} 已失效，请刷新合并依据")
    merged={d['conflict_id']:d for d in s.decisions}
    new_decisions=[]
    for item in incoming:
        v=_validate_decision(by_cid[item['conflict_id']],{**item,'at':now()})
        merged[v['conflict_id']]=v;new_decisions.append(v)
    all_decisions=list(merged.values())
    try:
        final_rows=apply_decisions(plan,all_decisions)  # raises while conflicts remain
    except ValueError as e:
        raise HTTPException(409,str(e))
    s.decisions=all_decisions
    plan['decisions']=all_decisions;s.plan=plan
    s.result_checksum=checksum([{'source_key':r['source_key'],'values':r['values']} for r in final_rows])
    s.status='resolved';s.updated_at=now()
    response=session_view(s)
    _store_idempotent(db,key,action,s.study_id,response)
    log(db,user,'merge.decide',f'{session_id}/decisions{len(new_decisions)}')
    db.commit()
    return response

def apply(db,session_id,data,user):
    action='merge.apply'
    s=get_session(db,session_id)
    lock_study(db,s.study_id)
    if s.status=='applied':
        existing=_idempotent(db,data.idempotency_key,action)
        db.commit()
        return (existing.response if existing else session_view(s))
    if s.status=='superseded': raise HTTPException(409,'该会话已被新会话取代，不能落地')
    if s.status in ('conflicts','awaiting'):
        raise HTTPException(409,'冲突尚未全部裁决，正式观测保持不变；请先完成裁决再落地')
    existing=_idempotent(db,data.idempotency_key,action)
    if existing: db.commit();return existing.response
    # Last-moment drift recompute; decisions survive only by fingerprint.
    _,base_sum=worksheet_snapshot(db,s.study_id)
    if base_sum!=s.base_checksum or not s.plan:
        _recompute(db,s)
        log(db,user,'merge.recompute',f'{session_id}/at-apply/drift{s.drift_revision}')
        if s.status!='resolved': raise HTTPException(409,'正式批次在裁决期间变化，产生新冲突，请重新裁决后再落地')
    try:
        final_rows=apply_decisions(s.plan,s.decisions)
    except ValueError as e:
        raise HTTPException(409,str(e))
    # Whole-batch atomic replacement inside this locked transaction.
    old=list(db.scalars(select(Observation).where(Observation.study_id==s.study_id)))
    for row in old: db.delete(row)
    db.flush()
    created=[]
    for r in final_rows:
        row=Observation(study_id=s.study_id,source_key=r['source_key'],values=r['values'])
        db.add(row);created.append(row)
    db.flush()
    # Re-verify uniqueness against the actual table constraint outcomes.
    keys=[r.source_key for r in created]
    if len(keys)!=len(set(keys)): raise HTTPException(409,'落地校验失败：编号重复')
    s.status='applied';s.updated_at=now()
    s.result_checksum=checksum([{'source_key':r['source_key'],'values':r['values']} for r in final_rows])
    response=session_view(s)
    _store_idempotent(db,data.idempotency_key,action,s.study_id,response)
    log(db,user,'merge.apply',f'{s.study_id}/session{session_id}/replaced{len(old)}->with{len(created)}')
    db.commit()
    return response

def list_sessions(db,study_id=None):
    stmt=select(MergeSession).order_by(MergeSession.id.desc())
    if study_id is not None: stmt=stmt.where(MergeSession.study_id==study_id)
    return [session_view(s) for s in db.scalars(stmt).all()]

def session_view(s):
    view=record(s)
    if s.plan:
        view['conflict_count']=len(s.plan['conflicts'])
        view['basis_count']=len(s.plan['basis'])
        view['reused_decisions']=s.plan.get('reused_decisions',[])
        view['dropped_decisions']=s.plan.get('dropped_decisions',[])
        view['package_meta']=s.plan.get('package_meta',{})
    else:
        view['conflict_count']=0;view['basis_count']=0
        view['reused_decisions']=[];view['dropped_decisions']=[];view['package_meta']={}
    return view
