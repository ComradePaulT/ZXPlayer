"""Download publisher logos from the documented World of Spectrum API."""
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from artwork_wos import request_api

TAPE_EXTENSIONS={'.tap','.tzx','.pzx','.csw','.spc','.sta','.ltp','.wav'}
IMAGE_EXTENSIONS={'.png','.jpg','.jpeg','.webp','.bmp','.gif'}
COMPANY_SUFFIXES={'software','systems','consultants','games','entertainment','entertainments',
                  'limited','ltd','inc','corporation','corp','group','computing'}


def compact(text):
    text=unicodedata.normalize('NFKD',str(text).casefold())
    return ''.join(c for c in text if c.isalnum() and not unicodedata.combining(c))


def forms(name):
    words=re.findall(r'[A-Za-z0-9]+',unicodedata.normalize('NFKD',str(name)))
    result=[compact(' '.join(words))]
    while len(words)>1 and words[-1].casefold() in COMPANY_SUFFIXES:
        words.pop();result.append(compact(' '.join(words)))
    return [value for value in dict.fromkeys(result) if value]


def tape_publishers(root):
    result=set()
    for tape in (Path(root)/'tapes').rglob('*'):
        if not tape.is_file() or tape.suffix.casefold() not in TAPE_EXTENSIONS:continue
        year=re.search(r'\((?:(?:19|20)\d{2}|\?{4})\)',tape.stem)
        if not year:continue
        publisher=re.search(r'\(([^()]*)\)',tape.stem[year.end():])
        if publisher and publisher.group(1).strip():result.add(publisher.group(1).strip())
    return sorted(result,key=str.casefold)


def best_match(name, candidates):
    wanted=forms(name);ranked=[]
    for candidate in candidates:
        if not isinstance(candidate,dict) or not candidate.get('publisher'):continue
        actual=forms(candidate['publisher'])
        exact=any(a==b for a in wanted for b in actual)
        ratio=max((SequenceMatcher(None,a,b).ratio() for a in wanted for b in actual),default=0)
        ranked.append(((0 if exact else 1,-ratio,len(candidate['publisher'])),candidate,ratio))
    if not ranked:return None
    rank,candidate,ratio=min(ranked,key=lambda item:item[0])
    return candidate if rank[0]==0 or ratio>=0.78 else None


def lookup(name, root, opener=urlopen):
    data=request_api('publishers',{'publisher':'_'+name,'limit':10},root,opener)
    candidate=best_match(name,data.get('publishers',[]))
    if candidate and not candidate.get('logos') and candidate.get('label_from_string'):
        parent=request_api('publishers',{'publisher':'_'+candidate['label_from_string'],'limit':10},root,opener)
        parent_match=best_match(candidate['label_from_string'],parent.get('publishers',[]))
        if parent_match and parent_match.get('logos'):candidate=parent_match
    return candidate


def existing_keys(folder):
    keys=set()
    for image in folder.rglob('*'):
        if image.is_file() and image.suffix.casefold() in IMAGE_EXTENSIONS:keys.update(forms(image.stem))
    return keys


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+','-',name).strip(' .') or 'Publisher'


def download_logo(file_id, destination, opener=urlopen):
    if not re.fullmatch(r'[A-Za-z0-9]+',str(file_id)):raise ValueError('Invalid WOS logo ID.')
    url='https://worldofspectrum.org/files/thumb/'+str(file_id)+'/400/400'
    try:
        with opener(Request(url,headers={'User-Agent':'ZXPlayer/0.9 (personal archive browser)'}),timeout=25) as response:
            content_type=str(response.headers.get('Content-Type','')).split(';')[0].casefold()
            data=response.read(5*1024*1024+1)
        if content_type not in ('image/png','image/jpeg','image/gif','image/webp'):
            raise ValueError('WOS did not return an image.')
        if not data or len(data)>5*1024*1024:raise ValueError('WOS logo was empty or too large.')
        temporary=destination.with_suffix(destination.suffix+'.part')
        temporary.write_bytes(data);temporary.replace(destination)
    except HTTPError as exc:
        if exc.code==429:raise RuntimeError('WOS is limiting requests. Please try again later.') from None
        raise RuntimeError(f'WOS logo request failed (HTTP {exc.code}).') from None
    except (URLError,TimeoutError,OSError):
        raise RuntimeError('Could not reach WOS. Check your connection and try again.') from None


def fetch_publisher_logos(root, opener=urlopen):
    root=Path(root);folder=root/'images'/'publishers';folder.mkdir(parents=True,exist_ok=True)
    publishers=tape_publishers(root);known=existing_keys(folder)
    downloaded=[];missing=[];skipped=[]
    for publisher in publishers:
        if any(key in known for key in forms(publisher)):
            skipped.append(publisher);continue
        candidate=lookup(publisher,root,opener)
        logos=candidate.get('logos',[]) if candidate else []
        logo=next((item for item in logos if isinstance(item,dict) and item.get('id')),None)
        if not logo:
            missing.append(publisher);continue
        destination=folder/(safe_name(publisher)+'.png')
        download_logo(logo['id'],destination,opener)
        known.update(forms(publisher));downloaded.append(publisher)
        time.sleep(.08)
    report={'downloaded':downloaded,'already_present':skipped,'not_available':missing}
    (root/'cache').mkdir(parents=True,exist_ok=True)
    (root/'cache'/'publisher-logo-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    message=f'Downloaded {len(downloaded)} publisher logos from WOS.'
    if skipped:message+=f' {len(skipped)} already present.'
    if missing:message+=f' WOS had no logo for {len(missing)} publishers.'
    return {'message':message,**report}
