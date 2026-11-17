from typing import Optional,Any
from pydantic import BaseModel,Field

class PackageRow(BaseModel):
    id:Optional[int]=Field(default=None,gt=0)
    client_ref:Optional[str]=Field(default=None,max_length=120)
    source_key:str=Field(min_length=1,max_length=100)
    values:dict

class RevisionPackageInput(BaseModel):
    lab_name:str=Field(min_length=1,max_length=80)
    author:str=Field(min_length=1,max_length=120)
    revision:int=Field(default=1,ge=1)
    base_checksum:str=Field(min_length=8,max_length=64)
    idempotency_key:str=Field(min_length=8,max_length=120)
    rows:list[PackageRow]=Field(max_length=5000)
    note:str=Field(default='',max_length=2000)

class DecisionInput(BaseModel):
    conflict_id:str=Field(min_length=3,max_length=40)
    choice:Optional[str]=None
    rename_key:Optional[str]=Field(default=None,max_length=100)
    resolution:Optional[dict]=None

class AdjudicateInput(BaseModel):
    decisions:list[DecisionInput]=Field(min_length=1)
    idempotency_key:Optional[str]=Field(default=None,max_length=120)

class ApplyInput(BaseModel):
    idempotency_key:str=Field(min_length=8,max_length=120)
