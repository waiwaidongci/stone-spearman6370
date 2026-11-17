"""Unit tests for the pure three-way merge engine (no database required)."""
import os,sys
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..'))
from src.services.merge_engine import compute,apply_decisions,package_checksum

V1={'porosity':10.0,'absorption':2.0}
V2={'porosity':11.0,'absorption':2.0}
V3={'porosity':10.0,'absorption':3.0}
V4={'porosity':12.0,'absorption':4.0}

def base_rows():
    return [{'id':1,'source_key':'K1','values':V1},
            {'id':2,'source_key':'K2','values':V1},
            {'id':3,'source_key':'K3','values':V1},
            {'id':4,'source_key':'K4','values':V1}]

def pkg(rows,**kw):
    kw.setdefault('lab_name','lab');kw.setdefault('author','a');kw.setdefault('revision',1)
    kw.setdefault('checksum',package_checksum(rows));kw['rows']=rows
    return kw

def keys(plan,decisions=None):
    rows=apply_decisions(plan,decisions or plan['decisions'])
    return {r['identity']:(r['source_key'],r['values'],r['operation']) for r in rows}

def test_distinct_field_edits_merge_automatically():
    # A changes porosity of id1, B changes absorption of id1: merge both.
    a=pkg([{'id':1,'source_key':'K1','values':{'porosity':11.0,'absorption':2.0}},
           {'id':2,'source_key':'K2','values':V1},{'id':3,'source_key':'K3','values':V1},{'id':4,'source_key':'K4','values':V1}])
    b=pkg([{'id':1,'source_key':'K1','values':{'porosity':10.0,'absorption':3.0}},
           {'id':2,'source_key':'K2','values':V1},{'id':3,'source_key':'K3','values':V1},{'id':4,'source_key':'K4','values':V1}])
    plan=compute(base_rows(),a,b)
    assert plan['conflicts']==[]
    out=keys(plan)
    assert out['id-1'][1]=={'porosity':11.0,'absorption':3.0}

def test_rename_followed_by_one_sided_edit_tracks_identity():
    a=pkg([{'id':1,'source_key':'K1-X','values':V2},
           {'id':2,'source_key':'K2','values':V1},{'id':3,'source_key':'K3','values':V1},{'id':4,'source_key':'K4','values':V1}])
    b=pkg([{'id':1,'source_key':'K1','values':V1},
           {'id':2,'source_key':'K2','values':V1},{'id':3,'source_key':'K3','values':V1},{'id':4,'source_key':'K4','values':V1}])
    plan=compute(base_rows(),a,b)
    assert plan['conflicts']==[]
    assert keys(plan)['id-1'][0]=='K1-X'

def test_same_item_different_edits_need_adjudication():
    a=pkg([{'id':1,'source_key':'K1','values':V2}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    b=pkg([{'id':1,'source_key':'K1','values':V4}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    plan=compute(base_rows(),a,b)
    assert len(plan['conflicts'])==1 and plan['conflicts'][0]['kind']=='same_item'
    cid=plan['conflicts'][0]['conflict_id']
    try: apply_decisions(plan,[]) ;raise AssertionError('should reject unresolved conflict')
    except ValueError as e: assert '未裁决' in str(e)
    out=apply_decisions(plan,[{'conflict_id':cid,'choice':'B'}])
    row=next(r for r in out if r['identity']=='id-1')
    assert row['values']==V4

def test_rename_plus_other_side_value_edit_combines():
    # A renames id1, B edits one measurement: both land on the same identity.
    a=pkg([{'id':1,'source_key':'K1-X','values':V1}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    b=pkg([{'id':1,'source_key':'K1','values':V2}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    plan=compute(base_rows(),a,b)
    assert plan['conflicts']==[], plan['conflicts']
    row=keys(plan)['id-1']
    assert row[0]=='K1-X' and row[1]==V2

def test_overlapping_field_edits_still_conflict():
    a=pkg([{'id':1,'source_key':'K1','values':V2}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    b=pkg([{'id':1,'source_key':'K1','values':{'porosity':13.0,'absorption':3.0}}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    plan=compute(base_rows(),a,b)
    assert [c['kind'] for c in plan['conflicts']]==['same_item']

def test_delete_vs_modify_conflict_and_keep_or_delete():
    # A deletes id1, B modifies id1.
    a=pkg([{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    b=pkg([{'id':1,'source_key':'K1','values':V2}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    plan=compute(base_rows(),a,b)
    assert [c['kind'] for c in plan['conflicts']]==['delete_modify']
    cid=plan['conflicts'][0]['conflict_id']
    kept=apply_decisions(plan,[{'conflict_id':cid,'choice':'keep'}])
    assert any(r['identity']=='id-1' for r in kept)
    gone=apply_decisions(plan,[{'conflict_id':cid,'choice':'delete'}])
    assert not any(r['identity']=='id-1' for r in gone)

def test_both_delete_is_clean_and_delete_with_other_unchanged_is_clean():
    a=pkg([{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    b=pkg([{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    plan=compute(base_rows(),a,b)
    assert plan['conflicts']==[]
    assert 'id-1' not in keys(plan)

def test_key_swap_lands_without_conflict():
    # Both sides swap K1<->K2 keys on the same identities.
    rows=lambda swap:[{'id':1,'source_key':'K2' if swap else 'K1','values':V1},
                      {'id':2,'source_key':'K1' if swap else 'K2','values':V1},
                      {'id':3,'source_key':'K3','values':V1},{'id':4,'source_key':'K4','values':V1}]
    plan=compute(base_rows(),pkg(rows(True),lab_name='A'),pkg(rows(True),lab_name='B'))
    assert plan['conflicts']==[]
    out=keys(plan)
    assert out['id-1'][0]=='K2' and out['id-2'][0]=='K1'

def test_one_sided_rename_into_occupied_key_is_conflict():
    # A renames id1 to K2 while id2 keeps K2: occupation conflict.
    a=pkg([{'id':1,'source_key':'K2','values':V1},{'id':2,'source_key':'K2','values':V1},
           {'id':3,'source_key':'K3','values':V1},{'id':4,'source_key':'K4','values':V1}])
    b=pkg([{'id':i,'source_key':f'K{i}','values':V1} for i in (1,2,3,4)])
    plan=compute(base_rows(),a,b)
    assert any(c['kind']=='key_occupation' for c in plan['conflicts'])
    cid=next(c['conflict_id'] for c in plan['conflicts'] if c['kind']=='key_occupation')
    out=apply_decisions(plan,[{'conflict_id':cid,'resolution':{'id-1':'K1','id-2':'K2'}}])
    got={r['identity']:r['source_key'] for r in out}
    assert got['id-1']=='K1' and got['id-2']=='K2'

def test_add_collision_both_sides_same_new_key():
    extra_a={'client_ref':'x1','source_key':'NEW','values':V1}
    extra_b={'client_ref':'y9','source_key':'NEW','values':V2}
    base=base_rows()
    a=pkg([{'id':r['id'],'source_key':r['source_key'],'values':r['values']} for r in base]+[extra_a],lab_name='A')
    b=pkg([{'id':r['id'],'source_key':r['source_key'],'values':r['values']} for r in base]+[extra_b],lab_name='B')
    plan=compute(base,a,b)
    kinds=[c['kind'] for c in plan['conflicts']]
    assert 'add_collision' in kinds
    cid=next(c['conflict_id'] for c in plan['conflicts'] if c['kind']=='add_collision')
    out=apply_decisions(plan,[{'conflict_id':cid,'choice':'A'}])
    assert any(r['client_ref']=='A-x1' for r in out)
    out2=apply_decisions(plan,[{'conflict_id':cid,'choice':'drop'}])
    assert not any(r['identity'].startswith('new-') for r in out2)

def test_prior_decisions_reused_only_while_fingerprint_matches():
    a=pkg([{'id':1,'source_key':'K1','values':V2}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)],lab_name='A')
    b=pkg([{'id':1,'source_key':'K1','values':V4}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)],lab_name='B')
    plan=compute(base_rows(),a,b)
    assert [c['kind'] for c in plan['conflicts']]==['same_item']
    fp=plan['conflicts'][0]['fingerprint'];cid=plan['conflicts'][0]['conflict_id']
    prior=[{'conflict_id':cid,'fingerprint':fp,'choice':'A'}]
    # Unchanged three-way content: decision reused.
    plan2=compute(base_rows(),a,b,prior)
    assert plan2['reused_decisions'] and plan2['decisions'][0]['choice']=='A'
    # B changes its measurement: fingerprint changes, prior decision dropped.
    b2=pkg([{'id':1,'source_key':'K1','values':V3}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)],lab_name='B')
    plan3=compute(base_rows(),a,b2,prior)
    assert plan3['dropped_decisions'] and not plan3['decisions']

def test_same_state_equivalent_numbers_and_identical_edits_once():
    a=pkg([{'id':1,'source_key':'K1','values':{'porosity':10,'absorption':2}}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    b=pkg([{'id':1,'source_key':'K1','values':V1}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    plan=compute(base_rows(),a,b)
    assert [e['operation'] for e in plan['basis'] if e['identity']=='id-1']==['unchanged']
    a2=pkg([{'id':1,'source_key':'K1','values':V2}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    b2=pkg([{'id':1,'source_key':'K1','values':V2}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    plan2=compute(base_rows(),a2,b2)
    assert plan2['conflicts']==[] and keys(plan2)['id-1'][1]==V2

def test_single_side_package_treats_other_as_no_changes():
    a=pkg([{'id':1,'source_key':'K1','values':V2}]+[{'id':i,'source_key':f'K{i}','values':V1} for i in (2,3,4)])
    plan=compute(base_rows(),a,None)
    assert plan['conflicts']==[]
    assert keys(plan)['id-1'][1]==V2

def test_duplicate_final_keys_rejected_after_bad_resolution():
    a=pkg([{'id':1,'source_key':'K2','values':V1},{'id':2,'source_key':'K2','values':V1},
           {'id':3,'source_key':'K3','values':V1},{'id':4,'source_key':'K4','values':V1}])
    b=pkg([{'id':i,'source_key':f'K{i}','values':V1} for i in (1,2,3,4)])
    plan=compute(base_rows(),a,b)
    cid=next(c['conflict_id'] for c in plan['conflicts'] if c['kind']=='key_occupation')
    try:
        apply_decisions(plan,[{'conflict_id':cid,'resolution':{'id-1':'K9','id-2':'K9'}}])
        raise AssertionError('duplicate resolution must be rejected')
    except ValueError as e: assert '重复' in str(e)

def test_single_side_duplicate_keys_become_occupation_conflict():
    # Only one laboratory reports, but its renumbering makes two rows share K2.
    a=pkg([{'id':1,'source_key':'K2','values':V1},{'id':2,'source_key':'K2','values':V1},
           {'id':3,'source_key':'K3','values':V1},{'id':4,'source_key':'K4','values':V1}])
    plan=compute(base_rows(),a,None)
    assert [c['kind'] for c in plan['conflicts']]==['key_occupation']
    cid=plan['conflicts'][0]['conflict_id']
    out=apply_decisions(plan,[{'conflict_id':cid,'resolution':{'id-1':'K1','id-2':'K2'}}])
    got={r['identity']:r['source_key'] for r in out}
    assert got=={'id-1':'K1','id-2':'K2','id-3':'K3','id-4':'K4'}

def test_same_item_with_key_occupation_combined_adjudication():
    # id1 and id2 both edit the same field differently AND both target K9:
    # one same_item conflict plus one key_occupation conflict on two entries.
    a=pkg([{'id':1,'source_key':'K9','values':V2},{'id':2,'source_key':'K9','values':V2},
           {'id':3,'source_key':'K3','values':V1},{'id':4,'source_key':'K4','values':V1}],lab_name='A')
    b=pkg([{'id':1,'source_key':'K9','values':V4},{'id':2,'source_key':'K9','values':V4},
           {'id':3,'source_key':'K3','values':V1},{'id':4,'source_key':'K4','values':V1}],lab_name='B')
    plan=compute(base_rows(),a,b)
    kinds=sorted(c['kind'] for c in plan['conflicts'])
    assert kinds==['key_occupation','same_item','same_item']
    decisions=[]
    for c in plan['conflicts']:
        if c['kind']=='same_item': decisions.append({'conflict_id':c['conflict_id'],'choice':'A'})
        else:
            idents=sorted({cl['identity'] for cl in c['detail']['claimants']})
            decisions.append({'conflict_id':c['conflict_id'],
                              'resolution':{idents[0]:'K1',idents[1]:'K2'}})
    out=apply_decisions(plan,decisions)
    keys={r['identity']:r['source_key'] for r in out}
    assert keys['id-1']=='K1' and keys['id-2']=='K2'
    assert all(r['values']==V2 for r in out if r['identity'] in ('id-1','id-2'))

def test_drift_untouched_rows_inherit_new_formal_state():
    # Original base K1=V1; labs keep K1 at V1 while the formal worksheet drifts
    # K1 to V2. A lab-only edit on K2 must still merge, and K1 keeps V2.
    original=base_rows()
    drifted=[dict(r) for r in original];drifted[0]['values']=V2
    a=pkg([{'id':1,'source_key':'K1','values':V1},
           {'id':2,'source_key':'K2','values':V2},
           {'id':3,'source_key':'K3','values':V1},{'id':4,'source_key':'K4','values':V1}],lab_name='A')
    b=pkg([{'id':i,'source_key':f'K{i}','values':V1} for i in (1,2,3,4)],lab_name='B')
    plan=compute(drifted,a,b,original_base_rows=original)
    assert plan['conflicts']==[]
    out={r['identity']:(r['source_key'],r['values']) for r in apply_decisions(plan,[])}
    assert out['id-1']==('K1',V2)          # formal drift preserved
    assert out['id-2']==('K2',V2)          # lab edit still lands

def run_all():
    fns=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in fns: fn();print('PASS',fn.__name__)
    print(f'{len(fns)} engine tests passed')

if __name__=='__main__': run_all()
