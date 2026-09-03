# SPDX-License-Identifier: GPL-3.0-or-later
"""Live WOS Infoseek searches using its documented software/versions API."""
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, quote
from urllib.request import Request, urlopen
from artwork_online import download_candidates

API='https://worldofspectrum.org/infoseek/api/'


def api_key(root):
    try:settings=json.loads((Path(root)/'settings.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):settings={}
    return os.environ.get('ZXPLAYER_WOS_API_KEY') or settings.get('wos_api_key') or 'test'


def request_api(endpoint, params, root, opener=urlopen):
    params={**params,'X-API-KEY':api_key(root)}
    url=API+endpoint+'?'+urlencode(params)
    try:
        with opener(Request(url,headers={'User-Agent':'ZXPlayer/0.5 (personal cassette artwork viewer)'}),timeout=20) as response:
            raw=response.read(5*1024*1024+1)
        if len(raw)>5*1024*1024:raise ValueError('WOS returned an unexpectedly large response. Try a narrower search.')
        data=json.loads(raw)
        if not isinstance(data,dict):raise ValueError('WOS returned an unrecognised response.')
        return data
    except HTTPError as exc:
        if exc.code in (401,403):raise RuntimeError('WOS rejected the API key or request. Check your WOS API access.') from None
        if exc.code==429:raise RuntimeError('WOS is limiting requests. Please try again later.') from None
        raise RuntimeError(f'WOS request failed (HTTP {exc.code}). Please try again later.') from None
    except (URLError,TimeoutError,OSError):
        raise RuntimeError('Could not reach WOS. Check your connection or try again later.') from None
    except json.JSONDecodeError:
        raise RuntimeError('WOS did not return API data. Please try again later.') from None


def search_wos(query, category, root, opener=urlopen):
    if not query.strip():raise ValueError('Enter a title before searching WOS.')
    data=request_api('software',{'title':query.strip(),'limit':50,'offset':0},root,opener)
    titles=data.get('titles')
    if not isinstance(titles,list):raise ValueError('WOS did not return a title list. Check your API key.')
    results=[]
    for title in titles:
        if not str(title.get('id','')).isdigit() or not title.get('title'):continue
        publishers=', '.join(str(p.get('name','')) for p in title.get('publishers',[]) if isinstance(p,dict))
        results.append({'kind':'wos','id':int(title['id']),'title':title['title'],
                        'detail':'WOS · '+(publishers or 'Release details checked on download')})
    # The API lists games, not image availability. Only fetch versions after a choice.
    return {'results':results,'total':int(data.get('totalRecords') or len(results)),
            'warning':'','source':'wos'}


def file_category(file):
    path=str(file.get('file_id') or '')
    parsed=urlsplit(path)
    if parsed.scheme or parsed.netloc:return None
    if not path.startswith('/pub/sinclair/') or '..' in path.split('/'):return None
    if Path(path).suffix.lower() not in ('.jpg','.jpeg','.png','.gif','.webp'):return None
    lower=path.casefold()
    fmt=str(file.get('format') or '').casefold()
    if '/games-inlays/' in lower:
        if re.search(r'(?:_|-|\b)(back|rear)(?:[_.\-]|$)',Path(lower).name):return None
        return 'box'
    if any(s in fmt for s in ('cassette scan','tape scan','media scan')):
        return 'tape'
    # Classify a scan only when the archive's path identifies it explicitly.
    if any(s in lower for s in ('/games-tape-scans/','/games-cassette-scans/','/games-media/')):return 'tape'
    return None


def version_images(data, category):
    versions=data.get('versions')
    if not isinstance(versions,list):raise ValueError('WOS did not return a version list. Check your API key.')
    def number(v):
        try:return int(v.get('version'))
        except (ValueError,TypeError):return 9999
    unique={}
    for version in sorted(versions,key=number):
        seq=number(version)
        for file in version.get('files',[]) or []:
            if file_category(file)!=category:continue
            path=file['file_id']
            if path in unique:continue  # WOS repeats earlier files in later versions.
            original='original release' in str(file.get('origin') or '').casefold()
            rerelease='/rereleases/' in path.casefold()
            rank=0 if original and not rerelease else 1 if seq==1 and not rerelease else 2 if not rerelease else 3
            edition='WOS original release' if original and not rerelease else f'WOS version {seq}' if seq!=9999 else 'WOS release unspecified'
            if version.get('release_year'):edition+=' ('+str(version['release_year'])+')'
            unique[path]={'path':path,'url':'https://worldofspectrum.org'+quote(path,safe='/()'),
                'release':None,'edition':edition,'version_id':version.get('id'),
                '_rank':(rank,seq,'front' not in Path(path).stem.casefold(),path)}
    return sorted(unique.values(),key=lambda f:f['_rank'])


def download_wos(sid, category, root, opener=urlopen):
    if category not in ('box','tape'):raise ValueError('Unknown artwork category.')
    data=request_api('software_versions',{'sid':int(sid),'files':1},root,opener)
    images=version_images(data,category)
    if not images:
        raise ValueError('WOS lists no identifiable '+('cassette scan' if category=='tape' else 'front cover')+
                         ' for this title. Try Search catalogue or choose a local image.')
    game={'id':int(sid),'title':data['versions'][0].get('title','WOS title')}
    return download_candidates(game,images,category,root,opener,provider='WOS',snapshot='Live Infoseek API')
