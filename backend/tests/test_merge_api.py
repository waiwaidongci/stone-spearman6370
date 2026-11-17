"""End-to-end HTTP tests for the merge API using FastAPI TestClient (no Redis)."""
import os,sys
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..'))
os.environ.setdefault('JWT_SECRET','test-secret')
os.environ.setdefault('REDIS_URL','redis://localhost:6379/0')
os.environ.setdefault('DATABASE_URL','sqlite://')

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from src.config import database as dbcfg
from src.config.database import Base,get_db
from src.models import user,study,observation,run,review,audit,merge  # noqa: F401
from src.models.user import User
from src.models.study import Study
from src.models.observation import Observation
from src.routes import auth,research,merge as merge_routes
from src.utils.security import hash_password,token
from src.services import merge_sessions as ms

engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
TestSession=sessionmaker(bind=engine,expire_on_commit=False)
dbcfg.engine=engine;dbcfg.Session=TestSession

def build_app():
    app=FastAPI()
    app.include_router(auth.router);app.include_router(research.router)
    app.include_router(merge_routes.router)
    def override():
        s=TestSession()
        try: yield s
        finally: s.close()
    app.dependency_overrides[get_db]=override
    return app

@pytest.fixture()
def client():
    Base.metadata.create_all(engine)
    s=TestSession()
    u_admin=User(email='admin@demo.local',role='admin',password_hash=hash_password('x'))
    u_view=User(email='viewer@demo.local',role='viewer',password_hash=hash_password('x'))
    st=Study(name='HTTP批次',source_filename='b.csv',description='')
    s.add_all([u_admin,u_view,st]);s.flush()
    for i in range(1,4):
        s.add(Observation(study_id=st.id,source_key=f'K{i}',
                          values={'porosity':10.0,'absorption':2.0}))
    s.commit()
    admin_token=token(u_admin);viewer_token=token(u_view)
    app=build_app()
    c=TestClient(app)
    c.headers.update({'Authorization':f'Bearer {admin_token}'})
    c.admin_token=admin_token;c.viewer_token=viewer_token;c.study_id=st.id
    yield c
    Base.metadata.drop_all(engine)

def _base(c):
    rows=c.get(f"/studies/{c.study_id}/observations").json()
    base_sum=ms.worksheet_snapshot(TestSession(),c.study_id)[1]
    return rows,base_sum

def test_full_merge_workflow_over_http(client):
    rows,base_sum=_base(client)
    # A edits porosity of K1, B edits absorption of K1 -> automatic field merge.
    ra=[dict(r,values={'porosity':11.0,'absorption':2.0}) for r in rows]
    rb=[dict(r,values={'porosity':10.0,'absorption':3.0}) for r in rows]
    r1=client.post(f"/studies/{client.study_id}/merge-packages",
                   json={'lab_name':'北山实验室','author':'u1','revision':1,
                         'base_checksum':base_sum,'idempotency_key':'idempotency-http-a','rows':ra})
    assert r1.status_code==200 and r1.json()['status']=='awaiting'
    # Retry with the same idempotency key returns the identical session.
    r1b=client.post(f"/studies/{client.study_id}/merge-packages",
                    json={'lab_name':'北山实验室','author':'u1','revision':1,
                          'base_checksum':base_sum,'idempotency_key':'idempotency-http-a','rows':ra})
    assert r1b.json()['id']==r1.json()['id']
    r2=client.post(f"/studies/{client.study_id}/merge-packages",
                   json={'lab_name':'南麓实验室','author':'u2','revision':1,
                         'base_checksum':base_sum,'idempotency_key':'idempotency-http-b','rows':rb})
    assert r2.status_code==200 and r2.json()['status']=='resolved'
    sid=r2.json()['id']
    detail=client.get(f'/merge-sessions/{sid}').json()
    assert detail['basis_count']==3 and detail['conflict_count']==0
    # Basis is inspectable.
    basis=detail['plan']['basis']
    entry=next(e for e in basis if e['identity']=='id-1')
    assert entry['operation']=='modified'
    # Formal data untouched before apply.
    before=client.get(f"/studies/{client.study_id}/observations").json()
    assert before[0]['values']=={'porosity':10.0,'absorption':2.0}
    applied=client.post(f'/merge-sessions/{sid}/apply',json={'idempotency_key':'idempotency-apply-1'})
    assert applied.status_code==200 and applied.json()['status']=='applied'
    after=client.get(f"/studies/{client.study_id}/observations").json()
    assert after[0]['values']=={'porosity':11.0,'absorption':3.0}
    # Idempotent apply replay changes nothing.
    replay=client.post(f'/merge-sessions/{sid}/apply',json={'idempotency_key':'idempotency-apply-1'})
    assert replay.json()['status']=='applied'
    assert len(client.get(f"/studies/{client.study_id}/observations").json())==3

def test_conflict_adjudication_and_permissions_over_http(client):
    rows,base_sum=_base(client)
    ra=[dict(r,values={'porosity':11.0,'absorption':2.0} if r['source_key']=='K1' else r['values']) for r in rows]
    rb=[dict(r,values={'porosity':12.0,'absorption':2.0} if r['source_key']=='K1' else r['values']) for r in rows]
    client.post(f"/studies/{client.study_id}/merge-packages",
                json={'lab_name':'北山实验室','author':'u1','revision':1,
                      'base_checksum':base_sum,'idempotency_key':'idempotency-c-a','rows':ra})
    v=client.post(f"/studies/{client.study_id}/merge-packages",
                  json={'lab_name':'南麓实验室','author':'u2','revision':1,
                        'base_checksum':base_sum,'idempotency_key':'idempotency-c-b','rows':rb}).json()
    sid=v['id'];cid=v['plan']['conflicts'][0]['conflict_id']
    # Applying with unresolved conflicts is refused with 409.
    refused=client.post(f'/merge-sessions/{sid}/apply',json={'idempotency_key':'idempotency-c-app'})
    assert refused.status_code==409
    # Viewers cannot adjudicate.
    r=client.post(f'/merge-sessions/{sid}/adjudicate',
                  headers={'Authorization':f'Bearer {client.viewer_token}'},
                  json={'decisions':[{'conflict_id':cid,'choice':'B'}],
                        'idempotency_key':'idempotency-c-dec'})
    assert r.status_code==403
    # Writers adjudicate; only admin may apply.
    ok=client.post(f'/merge-sessions/{sid}/adjudicate',
                   json={'decisions':[{'conflict_id':cid,'choice':'B'}],
                         'idempotency_key':'idempotency-c-dec'})
    assert ok.status_code==200 and ok.json()['status']=='resolved'
    applied=client.post(f'/merge-sessions/{sid}/apply',json={'idempotency_key':'idempotency-c-app2'})
    assert applied.status_code==200
    got=client.get(f"/studies/{client.study_id}/observations").json()
    assert got[0]['values']=={'porosity':12.0,'absorption':2.0}
    # Listing endpoints work.
    assert any(x['id']==sid for x in client.get(f"/studies/{client.study_id}/merge-sessions").json())
    assert client.get('/merge-sessions').status_code==200

def test_stale_package_checksum_rejected_over_http(client):
    rows,_=_base(client)
    r=client.post(f"/studies/{client.study_id}/merge-packages",
                  json={'lab_name':'北山实验室','author':'u1','revision':1,
                        'base_checksum':'0'*64,'idempotency_key':'idempotency-stale','rows':rows})
    assert r.status_code==409 and '底稿' in r.json()['detail']

if __name__=='__main__':
    sys.exit(pytest.main([__file__,'-v','-p','no:cacheprovider']))
