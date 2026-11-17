from fastapi import APIRouter,Depends
from src.config.database import get_db
from src.middlewares.auth import current_user,writer,admin
from src.schemas.merge import RevisionPackageInput,AdjudicateInput,ApplyInput
from src.controllers import merge
router=APIRouter()

@router.post('/studies/{id}/merge-packages')
def upload_package(id:int,data:RevisionPackageInput,db=Depends(get_db),user=Depends(writer)):
    return merge.receive_package(db,id,data,user)

@router.get('/studies/{id}/merge-sessions')
def study_sessions(id:int,db=Depends(get_db),user=Depends(current_user)):
    return merge.list_sessions(db,id)

@router.get('/merge-sessions')
def all_sessions(db=Depends(get_db),user=Depends(current_user)):
    return merge.list_sessions(db,None)

@router.get('/merge-sessions/{session_id}')
def session_detail(session_id:int,db=Depends(get_db),user=Depends(current_user)):
    return merge.get_session(db,session_id)

@router.post('/merge-sessions/{session_id}/adjudicate')
def adjudicate(session_id:int,data:AdjudicateInput,db=Depends(get_db),user=Depends(writer)):
    return merge.adjudicate(db,session_id,data,user)

@router.post('/merge-sessions/{session_id}/apply')
def apply(session_id:int,data:ApplyInput,db=Depends(get_db),user=Depends(admin)):
    return merge.apply(db,session_id,data,user)
