import concurrent.futures as cf
import datetime, hashlib, json, pathlib, re, time
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

RUN = pathlib.Path(__file__).resolve().parent
RAW = RUN / 'source_evidence'
RAW.mkdir(exist_ok=True)
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'ResearchExploration/1.0 (academic bibliography verification)'})

def get(url):
    r = SESSION.get(url, timeout=40)
    r.raise_for_status()
    return r

def index(venue):
    url = f'https://openaccess.thecvf.com/{venue}?day=all'
    try:
        r = get(url)
        soup = BeautifulSoup(r.text, 'html.parser')
        result = []
        for a in soup.select('dt.ptitle a'):
            title = a.get_text(' ', strip=True)
            if re.search(r'assembl|procedur|mistake|action segmentation|action segment|egocentric|conformance|temporal action|uncertainty', title, re.I):
                result.append({'title': title, 'url': urljoin(url, a['href']), 'venue': venue})
        (RAW / f'index_{venue}.json').write_text(json.dumps({'url':url,'accessed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'matches':result},ensure_ascii=False,indent=2),encoding='utf-8')
        return {'venue':venue,'count':len(result),'results':result}
    except Exception as e:
        return {'venue':venue,'error':str(e)}

def fetch_paper(item):
    url = item['url']
    try:
        r = get(url)
        soup = BeautifulSoup(r.text, 'html.parser')
        def meta(name):
            return [m.get('content','') for m in soup.select(f'meta[name="{name}"]')]
        title = meta('citation_title')
        authors = meta('citation_author')
        date = meta('citation_publication_date') or meta('citation_date') or meta('citation_online_date') or meta('dc.date')
        abstract = soup.select_one('#abstract')
        if not abstract:
            abstract = soup.select_one('blockquote.abstract')
        if not abstract:
            abstract = soup.select_one('.abstract')
        if not abstract:
            abstract = soup.select_one('section[aria-labelledby="Abs1"]')
        if not abstract:
            abstract = soup.select_one('div[role="doc-abstract"]')
        record = dict(item)
        record.update({'canonical_title':title[0] if title else item.get('title'), 'authors':authors,'publication_date':date[0] if date else None,'accessed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(), 'fetched_url':r.url,'http_status':r.status_code,'abstract':abstract.get_text(' ',strip=True) if abstract else None})
        slug = re.sub(r'[^a-z0-9]+','_',record['canonical_title'].lower()).strip('_')[:90]+'_'+hashlib.sha1(url.encode()).hexdigest()[:8]
        record['html_evidence']='source_evidence/'+slug+'.html'
        record['json_evidence']='source_evidence/'+slug+'.json'
        metatable=soup.select_one('.metatable')
        record['venue_evidence']=metatable.get_text(' ',strip=True) if metatable else (meta('citation_conference_title') or meta('citation_journal_title'))
        (RAW / (slug+'.html')).write_text(r.text,encoding='utf-8')
        (RAW / (slug+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        return record
    except Exception as e:
        return {**item,'error':str(e)}

if __name__=='__main__':
    import sys
    if sys.argv[1]=='indexes':
        with cf.ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(index,sys.argv[2:]))
        print(json.dumps(results,ensure_ascii=False,indent=2))
    elif sys.argv[1]=='papers':
        items=json.loads((RUN/'selected_urls.json').read_text(encoding='utf-8'))
        with cf.ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(fetch_paper,items))
        (RUN/'verified_sources.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps([{k:v for k,v in r.items() if k!='abstract'} for r in results],ensure_ascii=False,indent=2))
