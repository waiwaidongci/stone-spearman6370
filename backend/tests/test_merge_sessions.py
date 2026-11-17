"""Integration tests for merge sessions against an in-memory SQLite database."""
import os,sys
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..'))
os.environ.setdefault('JWT_SECRET','test-secret')
os.environ.setdefault('REDIS_URL','redis://localhost:6379/0')
os.environ.setdefault('DATABASE_URL','sqlite://')

import pytest
from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from src.config import database as dbcfg
from src.models.user import User
from src.models.study import Study
from src.models.observation import Observation
from src.models.merge import MergeSession,IdempotencyRecord
from src.schemas.merge import RevisionPackageInput,AdjudicateInput,DecisionInput,ApplyInput
from src.services import merge_sessions as ms
from src.services import studies
from src.utils.security import hash_password
from src.utils.serializers import checksum

engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
dbcfg.engine=engine
dbcfg.Session=sessionmaker(bind=engine,expire_on_commit=False)

V1={'porosity':10.0,'absorption':2.0}

@pytest.fixture()
def db():
    from src.config.database import Base
    from src.models import user,study,observation,run,review,audit,merge  # noqa: F401
    Base.metadata.create_all(engine)
    s=dbcfg.Session()
    admin=User(email='admin@demo.local',role='admin',password_hash=hash_password('x'))
    analyst=User(email='analyst@demo.local',role='analyst',password_hash=hash_password('x'))
    s.add_all([admin,analyst]);s.flush()
    study=Study(name='批次甲',source_filename='base.csv',description='')
    s.add(study);s.flush()
    for i in range(1,5):
        s.add(Observation(study_id=study.id,source_key=f'K{i}',values=dict(V1)))
    s.commit()
    yield s
    s.rollback();s.close()
    Base.metadata.drop_all(engine)

def pkg_rows(rows):
    return [{'id':r['id'],'source_key':r['source_key'],'values':r['values']} for r in rows]

def make_package(lab,author,rows,key,base_sum,revision=1,note=''):
    return RevisionPackageInput(lab_name=lab,author=author,revision=revision,
                                base_checksum=base_sum,idempotency_key='idempotency-'+key,rows=rows,note=note)

def current_rows(db,study_id):
    return [(r.source_key,r.values) for r in db.scalars(
        select(Observation).where(Observation.study_id==study_id).order_by(Observation.id))]

def test_happy_path_distinct_edits_then_atomic_apply(db):
    study=db.scalar(select(Study))
    base,base_sum=ms.worksheet_snapshot(db,study.id)
    rows_a=pkg_rows(base);rows_a[0]['values']={'porosity':11.0,'absorption':2.0}
    rows_b=pkg_rows(base);rows_b[0]['values']={'porosity':10.0,'absorption':3.0}
    v1=ms.receive_package(db,study.id,make_package('北山实验室','u1',rows_a,'key-a',base_sum),
                          db.get(User,1))
    assert v1['status']=='awaiting'
    v2=ms.receive_package(db,study.id,make_package('南麓实验室','u2',rows_b,'key-b',base_sum),
                          db.get(User,2))
    assert v2['status']=='resolved' and v2['conflict_count']==0
    sid=v2['id']
    # Formal worksheet untouched before apply.
    assert current_rows(db,study.id)[0]==('K1',V1)
    out=ms.apply(db,sid,ApplyInput(idempotency_key='apply-one-001'),db.get(User,1))
    assert out['status']=='applied'
    merged=current_rows(db,study.id)
    assert merged[0]==('K1',{'porosity':11.0,'absorption':3.0})
    assert len(merged)==4
    # Replays are idempotent: same view, no duplicate rows/sessions.
    again=ms.apply(db,sid,ApplyInput(idempotency_key='apply-one-001'),db.get(User,1))
    assert again['id']==sid and len(current_rows(db,study.id))==4
    assert db.scalar(select(func.count()).select_from(MergeSession))==1

def test_package_retry_is_idempotent_and_never_duplicates(db):
    study=db.scalar(select(Study))
    base,base_sum=ms.worksheet_snapshot(db,study.id)
    payload=make_package('北山实验室','u1',pkg_rows(base),'dup-key',base_sum)
    first=ms.receive_package(db,study.id,payload,db.get(User,2))
    second=ms.receive_package(db,study.id,payload,db.get(User,2))
    assert first['id']==second['id']
    assert db.scalar(select(func.count()).select_from(MergeSession))==1
    assert db.scalar(select(func.count()).select_from(IdempotencyRecord))==1
    # A different key but identical content still joins the same session slot.
    rows2=pkg_rows(base);rows2[0]['source_key']='K1-X'
    other=make_package('北山实验室','u1',rows2,'other-key',base_sum,revision=2)
    v=ms.receive_package(db,study.id,other,db.get(User,2))
    assert v['id']==first['id']
    # Stale revision number cannot overwrite.
    stale=make_package('北山实验室','u1',pkg_rows(base),'stale-key',base_sum,revision=1)
    with pytest.raises(Exception) as ei: ms.receive_package(db,study.id,stale,db.get(User,2))
    assert '409' in str(ei.value)

def test_conflict_holds_formal_data_until_adjudicated_and_applied(db):
    study=db.scalar(select(Study))
    base,base_sum=ms.worksheet_snapshot(db,study.id)
    rows_a=pkg_rows(base);rows_a[0]['values']={'porosity':11.0,'absorption':2.0}
    rows_b=pkg_rows(base);rows_b[0]['values']={'porosity':12.0,'absorption':2.0}
    ms.receive_package(db,study.id,make_package('北山实验室','u1',rows_a,'ka',base_sum),db.get(User,2))
    view=ms.receive_package(db,study.id,make_package('南麓实验室','u2',rows_b,'kb',base_sum),db.get(User,2))
    assert view['status']=='conflicts' and view['conflict_count']==1
    cid=view['plan']['conflicts'][0]['conflict_id']
    assert view['plan']['conflicts'][0]['kind']=='same_item'
    # Unresolved apply is refused and the formal worksheet stays intact.
    with pytest.raises(Exception) as ei: ms.apply(db,view['id'],ApplyInput(idempotency_key='apply-pending-1'),db.get(User,1))
    assert '409' in str(ei.value)
    assert current_rows(db,study.id)[0]==('K1',V1)
    decided=ms.adjudicate(db,view['id'],
        AdjudicateInput(decisions=[DecisionInput(conflict_id=cid,choice='B')],idempotency_key='decide-d1-001'),
        db.get(User,2))
    assert decided['status']=='resolved'
    ms.apply(db,view['id'],ApplyInput(idempotency_key='apply-second-1'),db.get(User,1))
    assert current_rows(db,study.id)[0]==('K1',{'porosity':12.0,'absorption':2.0})

def test_drift_during_adjudication_recomputes_and_keeps_valid_decisions(db):
    study=db.scalar(select(Study))
    base,base_sum=ms.worksheet_snapshot(db,study.id)
    # Conflict on K1; K2 clean one-sided edit.
    rows_a=pkg_rows(base);rows_a[0]['values']={'porosity':11.0,'absorption':2.0}
    rows_b=pkg_rows(base);rows_b[0]['values']={'porosity':12.0,'absorption':2.0}
    ms.receive_package(db,study.id,make_package('北山实验室','u1',rows_a,'ka',base_sum),db.get(User,2))
    view=ms.receive_package(db,study.id,make_package('南麓实验室','u2',rows_b,'kb',base_sum),db.get(User,2))
    cid=view['plan']['conflicts'][0]['conflict_id']
    ms.adjudicate(db,view['id'],
        AdjudicateInput(decisions=[DecisionInput(conflict_id=cid,choice='A')],idempotency_key='decide-d1-001'),
        db.get(User,2))
    # Formal batch changes on an unrelated row while adjudication sits open.
    from src.schemas.observation import ObservationInput
    studies.save_observation(db,study.id,
        ObservationInput(source_key='K2',values={'porosity':99.0,'absorption':2.0}),db.get(User,1),row_id=2)
    again=ms.get_session(db,view['id'])
    out=ms.adjudicate(db,view['id'],
        AdjudicateInput(decisions=[DecisionInput(conflict_id=cid,choice='A')],idempotency_key='decide-d2-002'),
        db.get(User,2))
    assert out['drift_revision']>=1
    # Fingerprint of the K1 conflict is unchanged (drift was on K2): decision reused.
    assert cid in out['plan']['reused_decisions']
    assert out['status']=='resolved'
    ms.apply(db,view['id'],ApplyInput(idempotency_key='apply-pending-1'),db.get(User,1))
    final=dict(current_rows(db,study.id))
    assert final['K1']=={'porosity':11.0,'absorption':2.0}  # adjudicated
    assert final['K2']=={'porosity':99.0,'absorption':2.0}  # formal drift preserved

def test_drift_that_changes_conflict_content_drops_prior_decision(db):
    study=db.scalar(select(Study))
    base,base_sum=ms.worksheet_snapshot(db,study.id)
    rows_a=pkg_rows(base);rows_a[0]['values']={'porosity':11.0,'absorption':2.0}
    rows_b=pkg_rows(base);rows_b[0]['values']={'porosity':12.0,'absorption':2.0}
    ms.receive_package(db,study.id,make_package('北山实验室','u1',rows_a,'ka',base_sum),db.get(User,2))
    view=ms.receive_package(db,study.id,make_package('南麓实验室','u2',rows_b,'kb',base_sum),db.get(User,2))
    cid=view['plan']['conflicts'][0]['conflict_id']
    ms.adjudicate(db,view['id'],
        AdjudicateInput(decisions=[DecisionInput(conflict_id=cid,choice='A')],idempotency_key='decide-d1-001'),
        db.get(User,2))
    # Formal edit on the conflicted row itself changes its base state.
    from src.schemas.observation import ObservationInput
    studies.save_observation(db,study.id,
        ObservationInput(source_key='K1',values={'porosity':50.0,'absorption':2.0}),db.get(User,1),row_id=1)
    s=ms.get_session(db,view['id'])
    # Recompute happens on the next adjudication; the old fingerprint no longer
    # matches so the stored prior decision is dropped (visible in the response).
    out=ms.adjudicate(db,view['id'],
        AdjudicateInput(decisions=[DecisionInput(conflict_id=cid,choice='A')],idempotency_key='decide-d2-002'),
        db.get(User,2))
    assert out['plan']['dropped_decisions']
    # The re-raised conflict carries the drifted base as its common worksheet.
    assert out['plan']['conflicts'][0]['detail']['base']['values']=={'porosity':50.0,'absorption':2.0}
    ms.apply(db,view['id'],ApplyInput(idempotency_key='apply-drift-001'),db.get(User,1))
    final=dict(current_rows(db,study.id))
    assert final['K1']=={'porosity':11.0,'absorption':2.0}

def test_stale_base_checksum_rejected(db):
    study=db.scalar(select(Study))
    base,_=ms.worksheet_snapshot(db,study.id)
    bad=make_package('北山实验室','u1',pkg_rows(base),'stale001','0'*64)
    with pytest.raises(Exception) as ei: ms.receive_package(db,study.id,bad,db.get(User,2))
    assert '409' in str(ei.value) and '底稿' in str(ei.value)

def test_swap_lands_and_rollback_keeps_formal_batch(db):
    study=db.scalar(select(Study))
    base,base_sum=ms.worksheet_snapshot(db,study.id)
    swapped=pkg_rows(base)
    swapped[0]['source_key'],swapped[1]['source_key']='K2','K1'
    ms.receive_package(db,study.id,make_package('北山实验室','u1',swapped,'ka',base_sum),db.get(User,2))
    view=ms.receive_package(db,study.id,make_package('南麓实验室','u2',swapped,'kb',base_sum),db.get(User,2))
    assert view['status']=='resolved',view['plan']['conflicts']
    before=current_rows(db,study.id)
    # Force a failure inside the replacement transaction: nothing may change.
    original_add=db.add
    def boom(obj):
        if isinstance(obj,Observation): raise RuntimeError('simulated write failure')
        return original_add(obj)
    db.add=boom
    with pytest.raises(RuntimeError):
        ms.apply(db,view['id'],ApplyInput(idempotency_key='apply-fail-1'),db.get(User,1))
    db.rollback();db.add=original_add
    assert current_rows(db,study.id)==before
    # Retry after interruption lands cleanly.
    out=ms.apply(db,view['id'],ApplyInput(idempotency_key='apply-ok-001'),db.get(User,1))
    assert out['status']=='applied'
    keys=[k for k,_ in current_rows(db,study.id)]
    assert keys[:2]==['K2','K1'] or set(keys[:2])=={'K1','K2'}

def test_delete_modify_and_key_occupation_flow(db):
    study=db.scalar(select(Study))
    base,base_sum=ms.worksheet_snapshot(db,study.id)
    # A withdraws K1; B edits K1. A also renames K3 onto K4 (occupation).
    rows_a=[r for r in pkg_rows(base) if r['source_key']!='K1']
    rows_a[1]['source_key']='K4'  # was K3, now collides with surviving K4
    rows_b=pkg_rows(base);rows_b[0]['values']={'porosity':11.0,'absorption':2.0}
    ms.receive_package(db,study.id,make_package('北山实验室','u1',rows_a,'ka',base_sum),db.get(User,2))
    view=ms.receive_package(db,study.id,make_package('南麓实验室','u2',rows_b,'kb',base_sum),db.get(User,2))
    kinds=sorted(c['kind'] for c in view['plan']['conflicts'])
    assert kinds==['delete_modify','key_occupation']
    decisions=[]
    for c in view['plan']['conflicts']:
        if c['kind']=='delete_modify':
            decisions.append(DecisionInput(conflict_id=c['conflict_id'],choice='keep'))
        else:
            idents={cl['identity'] for cl in c['detail']['claimants']}
            decisions.append(DecisionInput(conflict_id=c['conflict_id'],
                resolution={ident:f'NEW-{n}' for n,ident in enumerate(sorted(idents))}))
    out=ms.adjudicate(db,view['id'],AdjudicateInput(decisions=decisions,idempotency_key='decide-multi-1'),db.get(User,2))
    assert out['status']=='resolved'
    ms.apply(db,view['id'],ApplyInput(idempotency_key='apply-pending-1'),db.get(User,1))
    final=dict(current_rows(db,study.id))
    assert 'K1' in final and final['K1']=={'porosity':11.0,'absorption':2.0}
    assert len(set(final))==len(final)

def test_old_runs_keep_their_frozen_snapshots_after_merge(db):
    study=db.scalar(select(Study))
    from src.models.run import Run
    snap=[{'source_key':'K1','values':V1}]
    run=Run(study_id=study.id,name='旧研究',status='completed',parameters={'resamples':4999},
            snapshot=snap,checksum=checksum(snap),result={'metrics':{}},error='',created_at='t')
    db.add(run);db.commit()
    base,base_sum=ms.worksheet_snapshot(db,study.id)
    rows_a=pkg_rows(base);rows_a[0]['values']={'porosity':11.0,'absorption':2.0}
    ms.receive_package(db,study.id,make_package('北山实验室','u1',rows_a,'ka',base_sum),db.get(User,2))
    view=ms.receive_package(db,study.id,make_package('南麓实验室','u2',rows_a,'kb',base_sum),db.get(User,2))
    ms.apply(db,view['id'],ApplyInput(idempotency_key='apply-pending-1'),db.get(User,1))
    stored=db.get(Run,run.id)
    assert stored.snapshot==snap  # historical research remains untouched

def test_two_labs_concurrent_intake_joins_one_session_without_loss(db):
    study=db.scalar(select(Study))
    base,base_sum=ms.worksheet_snapshot(db,study.id)
    rows_a=pkg_rows(base);rows_a[0]['values']={'porosity':11.0,'absorption':2.0}
    rows_b=pkg_rows(base);rows_b[0]['values']={'porosity':10.0,'absorption':3.0}
    # Interleave intake the way two retried HTTP requests could arrive; both
    # must converge on a single session and neither edit may be lost.
    va=ms.receive_package(db,study.id,make_package('北山实验室','u1',rows_a,'ka',base_sum),db.get(User,2))
    vb=ms.receive_package(db,study.id,make_package('南麓实验室','u2',rows_b,'kb',base_sum),db.get(User,2))
    assert va['id']==vb['id']
    assert db.scalar(select(func.count()).select_from(MergeSession))==1
    s=ms.get_session(db,va['id'])
    assert s.revision_a['lab_name']=='北山实验室' and s.revision_b['lab_name']=='南麓实验室'
    assert s.status=='resolved'
    ms.apply(db,s.id,ApplyInput(idempotency_key='apply-concurrent-1'),db.get(User,1))
    assert dict(current_rows(db,study.id))['K1']=={'porosity':11.0,'absorption':3.0}

def test_new_rows_with_client_refs_are_added_once(db):
    study=db.scalar(select(Study))
    base,base_sum=ms.worksheet_snapshot(db,study.id)
    extra={'client_ref':'lab-a-new-1','source_key':'K9','values':{'porosity':20.0,'absorption':4.0}}
    rows_a=pkg_rows(base)+[extra]
    va=ms.receive_package(db,study.id,make_package('北山实验室','u1',rows_a,'ka',base_sum),db.get(User,2))
    vb=ms.receive_package(db,study.id,make_package('南麓实验室','u2',pkg_rows(base),'kb',base_sum),db.get(User,2))
    assert va['id']==vb['id'] and vb['status']=='resolved'
    # Replaying A's package must not create the new row a second time.
    replay=ms.receive_package(db,study.id,make_package('北山实验室','u1',rows_a,'ka',base_sum),db.get(User,2))
    assert replay['id']==va['id']
    ms.apply(db,va['id'],ApplyInput(idempotency_key='apply-newrows-1'),db.get(User,1))
    keys=[k for k,_ in current_rows(db,study.id)]
    assert keys.count('K9')==1 and len(keys)==5

if __name__=='__main__':
    sys.exit(pytest.main([__file__,'-v','-p','no:cacheprovider']))
