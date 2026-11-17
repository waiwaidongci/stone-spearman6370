from src.services import merge_sessions as ms

def receive_package(db,study_id,data,user): return ms.receive_package(db,study_id,data,user)
def list_sessions(db,study_id): return ms.list_sessions(db,study_id)
def get_session(db,session_id): return ms.session_view(ms.get_session(db,session_id))
def adjudicate(db,session_id,data,user): return ms.adjudicate(db,session_id,data,user)
def apply(db,session_id,data,user): return ms.apply(db,session_id,data,user)
