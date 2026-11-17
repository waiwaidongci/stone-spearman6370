"""Three-way merge engine over observation worksheets.

Pure functions only: the engine never touches the database, so the same logic
serves API requests, retry handling and unit tests.

Identity model
--------------
Each base row has a stable integer ``id``. Revision packages keep that id for
rows derived from the common base worksheet; freshly added rows carry a
``client_ref`` string instead. A rename therefore follows the identity and is
never mistaken for a delete plus an add.

A "state" is the normalized dict ``{'source_key', 'values'}``; values compare
on canonical JSON form so 1 and 1.0 are the same measurement.

Conflict kinds
--------------
same_item        both sides edited one identity into different states
delete_modify    one side withdrew the row, the other edited it
add_collision    both sides added rows carrying the same source_key
key_occupation   target keys of otherwise-clean rows collide (incl. swaps);
                 adjudication assigns a final key to every claiming identity
A plain key swap claims each target key exactly once and therefore lands
without any conflict.
"""
import json
from src.utils.serializers import checksum

UNCHANGED='unchanged'
DELETED='deleted'
MODIFIED='modified'
ADDED='added'
CONFLICT='conflict'

def _norm_values(values):
    return json.loads(json.dumps(values,sort_keys=True,ensure_ascii=False,allow_nan=False))

def state_of(row):
    return {'source_key':row['source_key'],'values':_norm_values(row['values'])}

def same_state(x,y):
    return x is not None and y is not None and x==y

def _field_merge(base,a,b):
    """Combine per-field edits when the two sides touch disjoint attributes.

    A side "touches" a field only where its value differs from the base; the
    source key is treated as one more attribute. If both sides touch the same
    attribute with different results, the merge is impossible and a same_item
    conflict must be raised instead.
    """
    out=dict(base)
    for field in ('source_key','values'):
        if a[field]==b[field]:
            out[field]=a[field]
        elif a[field]==base[field]:
            out[field]=b[field]
        elif b[field]==base[field]:
            out[field]=a[field]
        else:
            if field!='values': return None
            merged=dict(base['values']);touched=set()
            for side in (a,b):
                for k,v in side['values'].items():
                    if v!=base['values'].get(k):
                        if k in touched and merged[k]!=v: return None
                        merged[k]=v;touched.add(k)
            out['values']=merged
    return out

def identity_ref(kind,token):
    return f'{kind}-{token}'

def parse_ref(ref):
    kind,token=ref.split('-',1)
    return kind,(int(token) if kind=='id' else token)

def fingerprint(kind,payload):
    return f'{kind}:{checksum(payload)}'[:72]

def index_side(rows):
    """Return base-id map and new-row map for one revision package."""
    by_id={};new={}
    for row in rows:
        rid=row.get('id')
        if rid is not None:
            by_id[int(rid)]=state_of(row)
        else:
            ref=row.get('client_ref') or f"{row['source_key']}|{checksum(state_of(row))[:10]}"
            new[str(ref)]=state_of(row)
    return by_id,new

def compute(base_rows, rev_a, rev_b, prior_decisions=None, original_base_rows=None):
    """Compute the three-way merge plan.

    ``base_rows``: official worksheet rows [{'id','source_key','values'}].
    ``rev_a``/``rev_b``: package payloads or None, each with ``rows`` and
    package metadata. Returns basis entries, conflicts and carried decisions.

    ``original_base_rows`` is used after drift: the formal worksheet changed
    while adjudication was in progress, so base rows now carry the new formal
    state while the packages still reference the original base. A row that
    neither laboratory touched relative to the original base inherits the
    drifted formal state rather than reverting it.
    """
    base_by_id={int(r['id']):state_of(r) for r in base_rows}
    original_by_id={int(r['id']):state_of(r) for r in original_base_rows} if original_base_rows else None
    sides={}
    for side,pkg in (('A',rev_a),('B',rev_b)):
        if pkg is None: sides[side]=None
        else:
            by_id,new=index_side(pkg.get('rows',[]))
            sides[side]={'by_id':by_id,'new':new,
                         'meta':{k:pkg.get(k) for k in ('lab_name','author','created_at','revision','checksum')}}
    basis=[];conflicts=[]
    entry_conflicts={}   # identity -> [conflict_id,...]
    def attach(ident,cid): entry_conflicts.setdefault(ident,[]).append(cid)

    # ---- 1. every identity present in the common base worksheet ----
    for rid,base in sorted(base_by_id.items()):
        ident=identity_ref('id',rid)
        ident_tuple=('id',rid)
        a=sides['A']['by_id'].get(rid) if sides['A'] else None
        b=sides['B']['by_id'].get(rid) if sides['B'] else None
        a_missing=sides['A'] is not None and rid not in sides['A']['by_id']
        b_missing=sides['B'] is not None and rid not in sides['B']['by_id']
        if original_by_id is not None:
            # Untouched lab rows (equal to the original base) inherit the
            # current formal state so drift is never silently reverted.
            old=original_by_id.get(rid)
            if a is not None and old is not None and same_state(a,old): a=base
            if b is not None and old is not None and same_state(b,old): b=base
        entry={'identity':ident,'base':base,'side_a':a,'side_b':b}
        if a_missing and b_missing:
            entry.update(operation=DELETED,reason='双方均撤回该试件',result=None)
        elif a_missing or b_missing:
            missing_side='A' if a_missing else 'B'
            kept=b if a_missing else a
            if same_state(kept,base):
                entry.update(operation=DELETED,reason=f'{missing_side}方撤回、另一方未改，执行撤回',result=None)
            else:
                cid=_conflict(conflicts,'delete_modify',ident_tuple,
                    {'base':base,'delete_side':missing_side,
                     'keep_side':'B' if a_missing else 'A','kept_state':kept})
                attach(ident,cid)
                entry.update(operation=CONFLICT,reason='撤回遇修改，待裁决',result=None)
        elif a is None:
            entry.update(operation=(UNCHANGED if same_state(b,base) else MODIFIED),
                         reason='甲修订包未包含该会话，按乙实验室结果落地',
                         result=base if same_state(b,base) else b)
        elif b is None:
            entry.update(operation=(UNCHANGED if same_state(a,base) else MODIFIED),
                         reason='乙修订包未包含该会话，按甲实验室结果落地',
                         result=base if same_state(a,base) else a)
        elif same_state(a,b):
            entry.update(operation=(UNCHANGED if same_state(a,base) else MODIFIED),
                         reason='双方未修改' if same_state(a,base) else '双方做了相同修改，只落地一次',
                         result=base if same_state(a,base) else a)
        elif same_state(a,base):
            entry.update(operation=MODIFIED,reason='仅乙实验室修改，自动接纳',result=b)
        elif same_state(b,base):
            entry.update(operation=MODIFIED,reason='仅甲实验室修改，自动接纳',result=a)
        else:
            merged_state=_field_merge(base,a,b) if (a is not None and b is not None) else None
            if merged_state is not None:
                entry.update(operation=MODIFIED,
                             reason='双方修改不同测值字段（含互不冲突的改名），字段级自动合并',result=merged_state)
            else:
                cid=_conflict(conflicts,'same_item',ident_tuple,{'base':base,'state_a':a,'state_b':b})
                attach(ident,cid)
                entry.update(operation=CONFLICT,reason='同项异改，待裁决',result=None)
        basis.append(entry)

    # ---- 2. rows added independently by either laboratory ----
    add_claims=[]
    for side in ('A','B'):
        if not sides[side]: continue
        for ref,st in sorted(sides[side]['new'].items()):
            add_claims.append((identity_ref('new',f'{side}-{ref}'),side,st))
    by_key={}
    for ident,side,st in add_claims: by_key.setdefault(st['source_key'],[]).append((ident,side,st))
    for key,claimants in sorted(by_key.items()):
        if len(claimants)>1:
            ident0=claimants[0][0]
            cid=_conflict(conflicts,'add_collision',parse_ref(ident0),
                {'source_key':key,'claims':[{'identity':i,'side':s,'state':st} for i,s,st in claimants]})
            for ident,side,st in claimants:
                attach(ident,cid)
                basis.append({'identity':ident,'base':None,
                              'side_a':st if side=='A' else None,'side_b':st if side=='B' else None,
                              'operation':CONFLICT,'reason':'双方新增编号占用，待裁决','result':None})
        else:
            ident,side,st=claimants[0]
            basis.append({'identity':ident,'base':None,
                          'side_a':st if side=='A' else None,'side_b':st if side=='B' else None,
                          'operation':ADDED,'reason':f'{side}方新增试件','result':st})

    # ---- 3. target-key occupation across all claiming identities ----
    # Every candidate state claims its key. A swap (A<->B) claims each key
    # exactly once, so swaps never produce a conflict here.
    claims={}
    kind_by_cid={c['conflict_id']:c['kind'] for c in conflicts}
    def claim(ident,key,state,source):
        claims.setdefault(key,[]).append({'identity':ident,'source':source,'state':state})
    for entry in basis:
        ident=entry['identity']
        if entry['operation']==DELETED: continue
        if entry['operation']==CONFLICT:
            # same_item rows keep both candidate keys in play; delete_modify
            # and add_collision rows make no key claim until adjudicated.
            if any(kind_by_cid.get(c)=='same_item' for c in entry_conflicts.get(ident,[])):
                if entry['side_a']: claim(ident,entry['side_a']['source_key'],entry['side_a'],'side_a')
                if entry['side_b']: claim(ident,entry['side_b']['source_key'],entry['side_b'],'side_b')
        elif entry['result'] is not None:
            claim(ident,entry['result']['source_key'],entry['result'],'survivor')
    for key,holders in sorted(claims.items()):
        idents=sorted({h['identity'] for h in holders})
        if len(idents)>1:
            cid=_conflict(conflicts,'key_occupation',parse_ref(idents[0]),
                {'source_key':key,'claimants':holders})
            for ident in idents: attach(ident,cid)
            # clean survivor rows become conflicts; rows already in a conflict
            # keep their original conflict and additionally carry this one.
            for entry in basis:
                if entry['identity'] in idents and entry['operation'] not in (CONFLICT,DELETED):
                    entry['operation']=CONFLICT;entry['reason']='编号占用，待裁决';entry['result']=None
    for entry in basis: entry['conflict_ids']=entry_conflicts.get(entry['identity'],[])

    # ---- 4. carry over prior adjudications whose three-way content is intact ----
    valid={c['fingerprint']:c['conflict_id'] for c in conflicts}
    decisions=[];reused=[];dropped=[]
    for d in prior_decisions or []:
        if d.get('fingerprint') in valid:
            d=dict(d);d['conflict_id']=valid[d['fingerprint']]
            decisions.append(d);reused.append(d['conflict_id'])
        else: dropped.append(d.get('conflict_id'))
    return {'basis':basis,'conflicts':conflicts,'decisions':decisions,
            'reused_decisions':reused,'dropped_decisions':dropped,
            'entry_conflicts':entry_conflicts,
            'package_meta':{s:(sides[s]['meta'] if sides[s] else None) for s in ('A','B')}}

def _conflict(conflicts,kind,ident,payload):
    kind_token,token=ident
    cid=f'C{len(conflicts)+1:03d}'
    conflicts.append({'conflict_id':cid,'kind':kind,'identity':identity_ref(kind_token,token),
                      'fingerprint':fingerprint(kind,payload),'detail':payload})
    return cid

def apply_decisions(plan,decisions):
    """Resolve a plan into final rows; raise ValueError while conflicts stay open."""
    by_cid={c['conflict_id']:c for c in plan['conflicts']}
    chosen={}
    for d in decisions:
        c=by_cid.get(d.get('conflict_id'))
        if not c: raise ValueError('裁决对应冲突已不存在，请刷新后重新裁决')
        chosen[c['conflict_id']]=_validate_decision(c,d)
    missing=sorted(cid for cid in by_cid if cid not in chosen)
    if missing: raise ValueError('仍有冲突未裁决：'+', '.join(missing))
    key_overrides={}  # identity -> final key, from key_occupation adjudications
    for cid,c in by_cid.items():
        if c['kind']=='key_occupation':
            for ident,key in chosen[cid]['resolution'].items(): key_overrides[ident]=key
    basis={e['identity']:e for e in plan['basis']}
    final=[]
    for entry in plan['basis']:
        ident=entry['identity'];kind,token=parse_ref(ident)
        if entry['operation']==DELETED: continue
        st=None
        if entry['operation']==CONFLICT:
            cids=entry['conflict_ids'];kinds=[by_cid[c]['kind'] for c in cids]
            if 'add_collision' in kinds:
                c=by_cid[next(c for c in cids if by_cid[c]['kind']=='add_collision')]
                choice=chosen[c['conflict_id']]['choice']
                if choice=='drop': continue
                own_side='A' if ident.startswith('new-A-') else 'B'
                if own_side!=choice: continue  # the losing laboratory's new row disappears
                st=dict(next(cl['state'] for cl in c['detail']['claims'] if cl['side']==choice))
                if chosen[c['conflict_id']].get('rename_key'): st['source_key']=chosen[c['conflict_id']]['rename_key']
            elif 'delete_modify' in kinds:
                c=by_cid[next(c for c in cids if by_cid[c]['kind']=='delete_modify')]
                if chosen[c['conflict_id']]['choice']=='delete': continue
                st=dict(c['detail']['kept_state'])
                if chosen[c['conflict_id']].get('rename_key'): st['source_key']=chosen[c['conflict_id']]['rename_key']
            elif 'same_item' in kinds:
                c=by_cid[next(c for c in cids if by_cid[c]['kind']=='same_item')]
                st=dict(c['detail']['state_a' if chosen[c['conflict_id']]['choice']=='A' else 'state_b'])
                if chosen[c['conflict_id']].get('rename_key'): st['source_key']=chosen[c['conflict_id']]['rename_key']
            else:
                # key_occupation alone: recover the candidate state from claims
                c=by_cid[next(c for c in cids if by_cid[c]['kind']=='key_occupation')]
                st=dict(next(cl['state'] for cl in c['detail']['claimants'] if cl['identity']==ident))
            if 'key_occupation' in kinds:
                st['source_key']=key_overrides[ident]
        else:
            st=dict(entry['result'])
            if ident in key_overrides: st['source_key']=key_overrides[ident]
        if st is None: raise ValueError(f'{ident} 的裁决无法落地')
        final.append({'identity':ident,'id':token if kind=='id' else None,
                      'client_ref':token if kind=='new' else None,
                      'source_key':st['source_key'],'values':st['values'],
                      'operation':entry['operation'],'reason':entry['reason']})
    keys=[r['source_key'] for r in final]
    if len(keys)!=len(set(keys)):
        dup=sorted({k for k in keys if keys.count(k)>1})
        raise ValueError('裁决后仍存在重复编号：'+', '.join(dup))
    return final

def _validate_decision(c,d):
    kind=c['kind'];out={'conflict_id':c['conflict_id'],'fingerprint':c['fingerprint']}
    rk=d.get('rename_key')
    if rk is not None and (not isinstance(rk,str) or not 1<=len(rk)<=100): raise ValueError(f"{c['conflict_id']} 新编号非法")
    if kind=='same_item':
        if d.get('choice') not in ('A','B'): raise ValueError(f"{c['conflict_id']} 须选择甲或乙实验室方案")
        out['choice']=d['choice']
    elif kind=='delete_modify':
        if d.get('choice') not in ('delete','keep'): raise ValueError(f"{c['conflict_id']} 须选择撤回或保留修改")
        out['choice']=d['choice']
    elif kind=='add_collision':
        if d.get('choice') not in ('A','B','drop'): raise ValueError(f"{c['conflict_id']} 须选择保留甲、保留乙或全部舍弃")
        out['choice']=d['choice']
    elif kind=='key_occupation':
        res=d.get('resolution')
        # One identity may claim the key from both candidate sides; dedupe.
        idents={cl['identity'] for cl in c['detail']['claimants']}
        if not isinstance(res,dict) or set(res)!=idents: raise ValueError(f"{c['conflict_id']} 须为每个占用方指定最终编号")
        if any(not isinstance(v,str) or not v for v in res.values()): raise ValueError(f"{c['conflict_id']} 编号为空")
        if len(set(res.values()))!=len(res): raise ValueError(f"{c['conflict_id']} 裁决后编号仍重复")
        out['resolution']=res
    if rk is not None: out['rename_key']=rk
    out['at']=d.get('at')
    return out

def package_checksum(rows):
    return checksum([{'id':r.get('id'),'client_ref':r.get('client_ref'),
                      'source_key':r['source_key'],'values':_norm_values(r['values'])} for r in rows])
