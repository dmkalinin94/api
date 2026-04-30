from __future__ import annotations
import re, time, uuid
from dataclasses import dataclass, field
from urllib.parse import quote
import requests
from requests import RequestException
from ad_worker import ADUser
from cnf import CONFIG, get_logger
logger = get_logger()
class KTalkUnavailableError(RuntimeError): pass
@dataclass(slots=True)
class KTalkUser:
    mention_id:str; display_name:str; post:str; deactivated:bool
@dataclass(slots=True)
class UserMatchResult:
    status:str; ad_user:ADUser|None=None; ktalk_user:KTalkUser|None=None; candidates:list[dict]=field(default_factory=list); reason:str=""

def normalize_value(value:str)->str:
    value=(value or '').strip().lower(); value=re.sub(r'\s+',' ',value); return value.replace('ё','е')
def build_ktalk_bearer(token:str)->str:
    token=str(token or '').strip();
    if not token:return ''
    return token if token.lower().startswith('bearer ') else f'Bearer {token}'
def _build_ktalk_headers(talk_host:str='', host:str='', bearer:str='')->dict[str,str]:
    return {"accept":"application/json","content-type":"application/json","authorization":bearer or build_ktalk_bearer(str(CONFIG.get('ktalk_bearer_token',''))),"talk-host":talk_host or str(CONFIG.get('ktalk_talk_host','')),"host":host or str(CONFIG.get('ktalk_host','')),"user-agent":str(CONFIG.get('ktalk_user_agent','autoalerter/1.0'))}

def search_ktalk_users(query:str, base_url:str, talk_host:str, host:str, bearer_token:str, verify_ssl:bool=True, request_timeout:int=15, limit:int=15)->list[KTalkUser]:
    try:r=requests.get(base_url,headers=_build_ktalk_headers(talk_host,host,build_ktalk_bearer(bearer_token)),params={"query":query,"limit":int(limit)},verify=verify_ssl,timeout=request_timeout)
    except RequestException as exc: raise KTalkUnavailableError('Kontur Talk lookup failed') from exc
    if not r.ok: raise KTalkUnavailableError('Kontur Talk lookup failed')
    items=(r.json() or {}).get('items',[])
    return [KTalkUser(mention_id=str(i.get('user_id','')).strip(),display_name=str(i.get('display_name','')).strip(),post=str(i.get('post','')).strip(),deactivated=bool(i.get('general_deactivated',False))) for i in items if isinstance(i,dict)]

def match_ktalk_user(ad_user:ADUser,candidates:list[KTalkUser])->UserMatchResult:
    for c in candidates:
        if not c.deactivated: return UserMatchResult(status='found',ad_user=ad_user,ktalk_user=c,reason='name_only_title_missing')
    return UserMatchResult(status='not_found',ad_user=ad_user,reason='no_candidates')

def find_ktalk_match_for_ad_user(ad_user:ADUser)->UserMatchResult:
    return match_ktalk_user(ad_user, search_ktalk_users(ad_user.login, CONFIG['ktalk_base_url'], CONFIG['ktalk_talk_host'], CONFIG['ktalk_host'], CONFIG['ktalk_bearer_token'], bool(CONFIG.get('verify_ssl',True)), int(CONFIG.get('request_timeout',15)), int(CONFIG.get('ktalk_limit',15))))

def fetch_ktalk_profile_by_mention_id(mention_id:str)->dict:
    base=str(CONFIG.get('ktalk_profile_base_url','')).rstrip('/')
    if not base:return {}
    try:r=requests.get(f"{base}/{quote(str(mention_id).strip(),safe='')}",headers=_build_ktalk_headers(),verify=bool(CONFIG.get('verify_ssl',True)),timeout=int(CONFIG.get('request_timeout',15)))
    except RequestException as exc: raise KTalkUnavailableError('Kontur Talk lookup failed') from exc
    return r.json() if r.ok and isinstance(r.json(),dict) else {}

DIRECT_ROOM_CACHE:dict[str,str]={}
def ensure_direct_room(ktalk_mention_id:str)->str:
    m=str(ktalk_mention_id).strip().lower()
    if m in DIRECT_ROOM_CACHE:return DIRECT_ROOM_CACHE[m]
    url=str(CONFIG.get('ktalk_matrix_base_url','')).rstrip('/')+str(CONFIG.get('ktalk_create_room_path','/createRoom'))
    r=requests.post(url,headers=_build_ktalk_headers(),json={"is_direct":True,"invite":[m],"preset":"trusted_private_chat"},verify=bool(CONFIG.get('verify_ssl',True)),timeout=int(CONFIG.get('request_timeout',15)))
    if r.status_code!=200: raise KTalkUnavailableError(f'KTalk createRoom failed status={r.status_code}')
    room_id=str((r.json() or {}).get('room_id') or '').strip()
    if not room_id: raise KTalkUnavailableError('KTalk createRoom returned empty room_id')
    DIRECT_ROOM_CACHE[m]=room_id; return room_id

def build_ktalk_message_payload(body:str, reply_to_event_id:str|None=None)->dict:
    p={"msgtype":"m.text","body":body,"m.mentions":{}}
    if reply_to_event_id:p["m.relates_to"]={"m.in_reply_to":{"event_id":reply_to_event_id}}
    return p

def send_ktalk_message(room_id:str, body:str, reply_to_event_id:str|None=None)->str:
    txn_id=f"m{int(time.time()*1000)}.{uuid.uuid4().hex[:6]}"
    url=str(CONFIG.get('ktalk_matrix_base_url','')).rstrip('/')+str(CONFIG.get('ktalk_send_message_path','/rooms/{room_id}/send/m.room.message/{txn_id}')).format(room_id=quote(room_id,safe=''),txn_id=txn_id)
    r=requests.put(url,headers=_build_ktalk_headers(),json=build_ktalk_message_payload(body,reply_to_event_id),verify=bool(CONFIG.get('verify_ssl',True)),timeout=int(CONFIG.get('request_timeout',15)))
    if r.status_code!=200: raise KTalkUnavailableError(f'KTalk API returned {r.status_code}')
    eid=str((r.json() or {}).get('event_id') or '').strip()
    if not eid: raise KTalkUnavailableError('KTalk send message response has no event_id')
    return eid
SEVERITY_HEADERS={"disaster":"🔴 Disaster","high":"🟤 High","average":"🟠 Average","warning":"🟡 Warning","info":"🔵 Info","not classified":"⚪ Not classified"}
def _norm_sev(v:str)->str:
    x=re.sub(r'\s+',' ',str(v or '').strip().lower()); return x if x in SEVERITY_HEADERS else 'not classified'
def build_start_message_text(payload:dict)->str:
    lines=[SEVERITY_HEADERS[_norm_sev(payload.get('severity'))],'',f"Время: {payload.get('event_time')}",f"Триггер: {payload.get('trigger_name')}",f"Хост: {payload.get('host_name')}"]
    hg=[str(x).strip() for x in (payload.get('host_groups') or []) if str(x).strip()]
    if hg: lines.append(f"Группы хоста: {', '.join(hg)}")
    if str(payload.get('operation_data') or '').strip(): lines.append(f"Operation data: {payload.get('operation_data')}")
    if str(payload.get('trigger_url') or '').strip(): lines += ['', 'Ссылка на триггер:', str(payload.get('trigger_url'))]
    lines += ['', f"Event ID: {payload.get('event_id')}"]
    return '\n'.join(lines)
def build_resolve_message_text(payload:dict)->str:
    return '\n'.join(['🟢 АВАРИЯ ЗАВЕРШЕНА','',f"Время: {payload.get('event_time')}",f"Триггер: {payload.get('trigger_name')}",f"Хост: {payload.get('host_name')}",f"Severity: {payload.get('severity')}",'',f"Event ID: {payload.get('event_id')}"])
