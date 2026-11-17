import {request,post} from './client.js';
export const studies={list:()=>request('/studies'),create:x=>post('/studies',x),rows:id=>request(`/studies/${id}/observations`),save:(id,x,row)=>row?request(`/studies/${id}/observations/${row}`,{method:'PUT',body:JSON.stringify(x)}):post(`/studies/${id}/observations`,x),remove:(id,row)=>request(`/studies/${id}/observations/${row}`,{method:'DELETE'})};
export const runs={list:()=>request('/runs'),create:x=>post('/runs',x),get:id=>request(`/runs/${id}`),review:(id,note)=>post(`/runs/${id}/review`,{note}),export:id=>request(`/runs/${id}/export`)};
export const merges={
  sessions:studyId=>request(`/studies/${studyId}/merge-sessions`),
  all:()=>request('/merge-sessions'),
  get:id=>request(`/merge-sessions/${id}`),
  upload:(studyId,payload)=>post(`/studies/${studyId}/merge-packages`,payload),
  decide:(id,payload)=>post(`/merge-sessions/${id}/adjudicate`,payload),
  apply:(id,idempotencyKey)=>post(`/merge-sessions/${id}/apply`,{idempotency_key:idempotencyKey}),
};
