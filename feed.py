from twisted.web import server, resource
from twisted.internet import reactor, endpoints
from twisted.web.client import HTTPClientFactory, _makeGetterFactory 
from twisted.web.server import NOT_DONE_YET

from scrapy.http.response.text import TextResponse
from scrapy.downloadermiddlewares.decompression import DecompressionMiddleware
from scrapy.selector import Selector

from scrapy.http import Headers
from scrapy.responsetypes import responsetypes

import w3lib.url
import w3lib.html

from lxml import etree
import re
from hashlib import md5

from feedgenerator import Rss201rev2Feed, Enclosure
import datetime

import MySQLdb
from settings import DATABASES, DOWNLOADER_USER_AGENT

url_hash_regexp = re.compile('(#.*)?$')

def _getPageFactory(url, contextFactory=None, *args, **kwargs):
    """
    Download a web page as a string.
    Download a page. Return a deferred, which will callback with a
    page (as a string) or errback with a description of the error.
    See L{HTTPClientFactory} to see what extra arguments can be passed.
    """
    return _makeGetterFactory(
        url,
        HTTPClientFactory,
        contextFactory=contextFactory,
        *args, **kwargs)

def _buildScrapyResponse(page_factory, body):
    status = int(page_factory.status)
    headers = Headers(page_factory.response_headers)
    respcls = responsetypes.from_args(headers=headers, url=page_factory.url)
    return respcls(url=page_factory.url, status=status, headers=headers, body=body)

def element_to_string(element):
    s = [element.text] if element.text else []
    for sub_element in element:
        s.append(etree.tostring(sub_element))
    if element.tail:    
        s.append(element.tail)
    return ''.join(s)

def _build_link(html, doc_url, url):
    base_url = w3lib.html.get_base_url(html, doc_url)
    return w3lib.url.urljoin_rfc(base_url, url)

def _buildFeed(response, feed_config):
    tree = response.selector._root.getroottree()

    # get data from html 
    items = []
    for node in tree.xpath(feed_config['xpath']):
        item = {}
        title_link = None
        for field_name in ['title', 'description']:
            if field_name in feed_config['fields']:
                element = node.xpath(feed_config['fields'][field_name])
                if element:
                    item[field_name] = element_to_string(element[0])
                    # get item link
                    if field_name == 'title':
                        anchor = element[0].xpath('ancestor-or-self::node()[name()="a"]')
                        if anchor and anchor[0].get('href'):
                            title_link = _build_link(response.body_as_unicode(), feed_config['uri'], anchor[0].get('href'))
                  
        if len(item) == len(feed_config['fields']): # all fields are required
            item['title_link'] = title_link
            items.append(item)

    #build feed
    feed = Rss201rev2Feed(
        title='Polite Pol: ' + feed_config['uri'],
        link=feed_config['uri'],
        description="Generated by PolitePol.com.\n"+\
            "Url: " + feed_config['uri'],
        language="en",
    )
    for item in items:
        title = item['title'] if 'title' in item else ''
        desc = item['description'] if 'description' in item else ''
        if item['title_link']: 
            link = item['title_link']
        else:
            link = url_hash_regexp.sub('#' + md5((title+desc).encode('utf-8')).hexdigest(), feed_config['uri'])
        feed.add_item(
            title = title,
            link = link,
            description = desc,
            #enclosure=Enclosure(fields[4], "32000", "image/jpeg") if  4 in fields else None, #"Image"
            pubdate=datetime.datetime.now()
        )
    return feed.writeString('utf-8')

def _downloadDone(response_str, request=None, page_factory=None, feed_config=None):
    response = _buildScrapyResponse(page_factory, response_str)

    response = DecompressionMiddleware().process_response(None, response, None)

    if (isinstance(response, TextResponse)):
        response_str = _buildFeed(response, feed_config)

    request.setHeader(b"Content-Type", b'text/xml')
    request.write(response_str)
    request.finish()

def _downloadError(error, request=None, page_factory=None):
    if DEBUG:
        request.write('Downloader error: ' + error.getErrorMessage())
        request.write('Traceback: ' + error.getTraceback())
    else:
        request.write('Something wrong')
        sys.stderr.write(datetime.datetime.now())
        sys.stderr.write('\n'.join('Downloader error: ' + error.getErrorMessage(), 'Traceback: ' + error.getTraceback()))
    request.finish()

def startFeedRequest(request, feed_id):
    # get url, xpathes
    creds = DATABASES['default']
    db = MySQLdb.connect(host=creds['HOST'], port=int(creds['PORT']), user=creds['USER'], passwd=creds['PASSWORD'], db=creds['NAME'])
    feed = {}
    with db:
        cur = db.cursor()
        cur.execute("""select f.uri, f.xpath, fi.name, ff.xpath from frontend_feed f
                       right join frontend_feedfield ff on ff.feed_id=f.id
                       left join frontend_field fi on fi.id=ff.field_id
                       where f.id=%s""", (feed_id,))
        rows = cur.fetchall()

        for row in rows:
            if not feed:
                feed['uri'] = row[0]
                feed['xpath'] = row[1]
                feed['fields'] = {}
            feed['fields'][row[2]] = row[3]

    if feed:
        page_factory = _getPageFactory(feed['uri'],
                headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, sdch',
                    'User-Agent': DOWNLOADER_USER_AGENT
                    },
                redirectLimit=5,
                timeout=10
                )
        d = page_factory.deferred
        d.addCallback(_downloadDone, request=request, page_factory=page_factory, feed_config=feed)
        d.addErrback(_downloadError, request=request, page_factory=page_factory)
    else:
        request.write('Feed generator error: config of feed is empty')
        request.finish()
    return
