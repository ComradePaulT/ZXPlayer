"""On-demand image downloads from ZXDB catalogue links; never downloads game data."""
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

MAX_IMAGE_BYTES = 16 * 1024 * 1024


def search_text(text):
    return ''.join(c for c in unicodedata.normalize('NFKD',text.casefold()) if c.isalnum())


def search_artwork(catalog_path, query, category, root):
    """Search local images and archive titles/aliases, without network requests."""
    if category not in ('box','tape'):raise ValueError('Unknown artwork category.')
    tokens=[search_text(t) for t in query.split() if search_text(t)]
    if not tokens:raise ValueError('Enter a game title first.')
    query_key=search_text(query)
    def matches(text):return all(t in search_text(text) for t in tokens)
    results=[]
    images=Path(root)/'images'
    for path in images.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in ('.png','.jpg','.jpeg','.gif','.webp','.bmp'):continue
        relative=path.relative_to(images).as_posix()
        folder=relative.split('/')[0].casefold()
        correct=folder in ('tape','tapes') if category=='tape' else folder=='box' or '/' not in relative
        if correct and matches(path.stem):
            results.append({'kind':'local','title':path.stem,'path':relative,'detail':'LOCAL · '+relative})
    # Keep local searching useful even if the archive file was not copied over.
    warning=''
    try:catalog=json.loads(Path(catalog_path).read_text(encoding='utf-8'))
    except (OSError,ValueError):
        catalog={'keys':{},'games':{}}
        warning='Archive catalogue unavailable. Copy artwork_catalog.json beside player.py.'
    ids=set()
    for key,values in catalog['keys'].items():
        if all(t in key for t in tokens):ids.update(str(i) for i in values)
    for game in catalog['games'].values():
        if (str(game['id']) in ids or matches(game['title'])) and game.get(category):
            original=any(i.get('release')==0 for i in game[category])
            detail=f"ARCHIVE · {game.get('year') or 'Year unknown'} · "
            detail+='Original listed' if original else 'Other release / edition unknown'
            results.append({'kind':'archive','id':game['id'],'title':game['title'],'detail':detail})
    results.sort(key=lambda r:(search_text(r['title'])!=query_key,r['kind']!='local',r['title'].casefold(),str(r.get('id',r.get('path')))))
    return {'results':results[:200],'total':len(results),'warning':warning}


def source_url(path):
    if path.startswith('/zxdb/'):
        return 'https://spectrumcomputing.co.uk' + quote(path, safe='/()')
    if path.startswith('/pub/'):
        return ('https://archive.org/download/World_of_Spectrum_June_2017_Mirror/'
                'World%20of%20Spectrum%20June%202017%20Mirror.zip/'
                'World%20of%20Spectrum%20June%202017%20Mirror/' + quote(path.lstrip('/'), safe='/()'))
    raise ValueError('This image does not have a supported archive URL.')


def choose_game(catalog, key, filename):
    ids=catalog['keys'].get(key,[])
    games=[catalog['games'][str(i)] for i in ids]
    if not games:
        raise ValueError('No archive entry was matched for this title. Add artwork manually for this game.')
    if len(games)>1:
        year=re.search(r'\((\d{4})\)',filename)
        same_year=[g for g in games if year and g.get('year')==int(year.group(1))]
        if len(same_year)==1:games=same_year
    if len(games)!=1:
        raise ValueError('Several archive games share this title. Automatic downloading was skipped to avoid using the wrong artwork.')
    return games[0]


def ranked_images(game, category):
    images=[r for r in game.get(category,[]) if r['path'].startswith(('/zxdb/','/pub/'))]
    def rank(item):
        seq=item.get('release')
        original=0 if seq==0 else 1 if seq is None else 2
        # Original release takes precedence even when its mirror is slower.
        return (original, seq if isinstance(seq,int) else 999,
                item.get('language') not in ('en',None),
                not item['path'].startswith('/zxdb/'),
                'front' not in Path(item['path']).stem.casefold(),item['path'])
    return sorted(images,key=rank)


def download_artwork(catalog_path, key, filename, category, root, opener=urlopen, entry_id=None):
    if category not in ('box','tape'):
        raise ValueError('Unknown artwork category.')
    catalog=json.loads(Path(catalog_path).read_text(encoding='utf-8'))
    game=catalog['games'].get(str(entry_id)) if entry_id is not None else choose_game(catalog,key,filename)
    if game is None:raise ValueError('The selected archive entry is no longer in the catalogue.')
    candidates=ranked_images(game,category)
    return download_candidates(game,candidates,category,root,opener,snapshot=catalog.get('snapshot'))


def download_candidates(game, candidates, category, root, opener=urlopen, provider='ZXDB', snapshot=None):
    if not candidates:
        raise ValueError('The archive catalogue has no '+category+' image for this game.')
    folder=Path(root)/'images'/category/'downloaded'
    folder.mkdir(parents=True,exist_ok=True)
    last=''
    for item in candidates[:12]:
        url=item.get('url') or source_url(item['path'])
        ext=Path(item['path']).suffix.lower()
        if ext not in ('.jpg','.jpeg','.png','.gif','.webp'):continue
        token=hashlib.sha256(url.encode()).hexdigest()[:12]
        path=folder/f'{provider}-{game["id"]}-{token}{ext}'
        try:
            if not path.exists():
                request=Request(url,headers={'User-Agent':'ZXPlayer/0.4 (personal cassette artwork viewer)'})
                with opener(request,timeout=20) as response:
                    length=response.headers.get('Content-Length')
                    if length and int(length)>MAX_IMAGE_BYTES:raise ValueError('Image exceeds the 16 MB limit.')
                    content=response.read(MAX_IMAGE_BYTES+1)
                if len(content)>MAX_IMAGE_BYTES:raise ValueError('Image exceeds the 16 MB limit.')
                valid=(content.startswith((b'\xff\xd8\xff',b'\x89PNG\r\n\x1a\n',b'GIF87a',b'GIF89a')) or
                       (content.startswith(b'RIFF') and content[8:12]==b'WEBP'))
                if not valid:raise ValueError('Archive returned something other than an image.')
                expected=item.get('md5')
                if expected and hashlib.md5(content).hexdigest()!=expected:
                    raise ValueError('Image differs from its catalogue checksum; download skipped.')
                temporary=path.with_suffix(path.suffix+'.partial')
                temporary.write_bytes(content)
                temporary.replace(path)
            sequence=item.get('release')
            edition=item.get('edition') or ('Original release' if sequence==0 else 'Release not specified' if sequence is None else f'Re-release {sequence}')
            provenance={'game':game['title'],'id':game['id'],'url':url,'release':sequence,
                        'catalogue':snapshot,'image':item['path'],'provider':provider,
                        'edition':edition,'version_id':item.get('version_id')}
            path.with_suffix(path.suffix+'.json').write_text(json.dumps(provenance,indent=2),encoding='utf-8')
            return {'path':path.relative_to(Path(root)/'images').as_posix(),'message':edition+' artwork downloaded'}
        except HTTPError as exc:
            if exc.code in (401,403,429):
                raise RuntimeError(f'The archive declined the request (HTTP {exc.code}). Try again later or add the image manually.') from exc
            last=str(exc)
        except (OSError,ValueError) as exc:
            last=str(exc)
    raise RuntimeError('Could not download an archive image. '+last)
