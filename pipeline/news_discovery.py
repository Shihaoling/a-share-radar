"""Read every public article link in one fetched list page; never click pagination."""
from urllib.parse import urljoin,urlsplit
import re
from bs4 import BeautifulSoup

def parse_news_links(raw, url):
    soup=BeautifulSoup(raw,'html.parser');found={}
    for a in soup.select('a[href]'):
        href=urljoin(url,a.get('href',''));p=urlsplit(href)
        title=a.get_text(' ',strip=True)
        if p.scheme not in ('http','https') or p.hostname not in ('finance.eastmoney.com','stock.eastmoney.com'):continue
        if not re.fullmatch(r'/a/\d{14,24}\.html',p.path) or len(title)<6:continue
        href='https://'+p.hostname+p.path
        found.setdefault(href,{'title':title,'url':href,'published_at':None,'summary':''})
    return list(found.values())
