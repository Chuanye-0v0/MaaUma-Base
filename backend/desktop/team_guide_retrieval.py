"""Bounded public research adapter. No account data, paid key, browser or AI call."""
from datetime import datetime, timezone
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
import time
import zlib
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
from core.team_requirements import assess_requirements, normalize_requirements, VENUES
from core.team_guides import article_evidence, load_sources, local_facts, source_profile
RSS_BYTES = 128 * 1024
ARTICLE_BYTES = 2 * 1024 * 1024
TOTAL_BYTES = 6 * 1024 * 1024
MAX_PAGES = 3
TOTAL_SECONDS = 16

class RetrievalError(ValueError):

    def __init__(self, code, message):
        self.code, self.message = (code, message)
        super().__init__(code)

def public_url(url):
    """Syntax/literal checks need no DNS; resolved addresses are checked at fetch."""
    try:
        if not isinstance(url, str) or not 1 <= len(url) <= 2048 or any((ord(c) < 32 for c in url)):
            raise ValueError()
        part = urlsplit(url)
        if part.scheme != 'https' or not part.hostname or part.username is not None or (part.password is not None):
            raise ValueError()
        if part.port not in (None, 443) or '\\' in url:
            raise ValueError()
        host = part.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        if host == 'localhost' or any((host.endswith(suffix) for suffix in ('.localhost', '.local', '.internal', '.lan', '.home', '.invalid', '.test'))):
            raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError()
        except ValueError:
            if ':' in host or re.fullmatch('[0-9.]+', host):
                raise
            if not re.fullmatch('[a-z0-9.-]+', host) or '.' not in host:
                raise ValueError()
        if any((key.casefold() in {'token', 'access_token', 'api_key', 'key', 'password', 'authorization', 'signature', 'sig'} for key, _ in parse_qsl(part.query, keep_blank_values=True))):
            raise ValueError()
        netloc = '[' + host + ']' if ':' in host else host
        path = quote(part.path or '/', safe="/:@!$&'()*+,;=-._~%")
        query = quote(part.query, safe="=&;:+,/?@!$'()*-._~%")
        return urlunsplit(('https', netloc, path, query, ''))
    except (ValueError, TypeError, UnicodeError):
        raise RetrievalError('invalid_public_url', '攻略链接需为无凭据的公网 HTTPS 地址，不能使用文件、内网或非标准端口。') from None

def _public_addresses(host, resolver, deadline, *, allow_fake_ip=False):
    results = queue.Queue(maxsize=1)

    def resolve():
        try:
            results.put((True, resolver(host, 443, type=socket.SOCK_STREAM)))
        except Exception:
            results.put((False, None))
    threading.Thread(target=resolve, daemon=True).start()
    try:
        ok, values = results.get(timeout=max(0.01, min(2, deadline - time.monotonic())))
    except queue.Empty:
        raise RetrievalError('dns_timeout', '公开来源域名解析超时。') from None
    if not ok:
        raise RetrievalError('dns_failed', '公开来源域名暂时无法解析。')
    addresses = list(dict.fromkeys((value[4][0] for value in values)))
    try:
        parsed = [ipaddress.ip_address(value) for value in addresses]
        fake_range = ipaddress.ip_network('198.18.0.0/15')
        fake = [address.version == 4 and address in fake_range for address in parsed]
        if allow_fake_ip and any(fake) and all((address.is_global or is_fake for address, is_fake in zip(parsed, fake))):
            return None
        if not addresses or any((not address.is_global for address in parsed)):
            raise ValueError()
    except ValueError:
        raise RetrievalError('non_public_address', '域名解析结果不是公网地址，本次不访问；请检查网络或提供其他攻略来源。') from None
    return sorted(addresses, key=lambda address: ':' in address)

class _PinnedHTTPSConnection(http.client.HTTPSConnection):

    def __init__(self, host, address, timeout):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, 443), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)

def _connection(host, address, timeout):
    if address is None:
        return http.client.HTTPSConnection(host, port=443, timeout=timeout, context=ssl.create_default_context())
    return _PinnedHTTPSConnection(host, address, timeout)

class _BoundedDecoder:
    """Bound each inflate operation; gzip members share the same output budget."""
    def __init__(self, encoding):
        self.encoding = encoding
        self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == 'gzip' else None
        self.prefix = b''

    def feed(self, block, limit, deadline, account):
        output = bytearray()
        if self.decoder is None:
            self.prefix += block
            if len(self.prefix) < 2:
                return b''
            first, second = self.prefix[:2]
            wrapped = first & 15 == 8 and (first * 256 + second) % 31 == 0
            self.decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
            block, self.prefix = self.prefix, b''
        try:
            while block:
                if time.monotonic() >= deadline:
                    raise RetrievalError('research_timeout', '本次公开资料检索已到时间上限。')
                if self.decoder.eof:
                    if self.encoding != 'gzip':
                        raise RetrievalError('invalid_compressed_content', '公开来源压缩数据包含无效尾随内容。')
                    self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                part = self.decoder.decompress(block, max(1, limit + 1 - len(output)))
                account(len(part))
                output.extend(part)
                if len(output) > limit:
                    return bytes(output)  # Caller accounts for this byte before rejecting oversize.
                if self.decoder.eof:
                    block = self.decoder.unused_data
                else:
                    block = self.decoder.unconsumed_tail
        except zlib.error:
            raise RetrievalError('invalid_compressed_content', '公开来源压缩数据损坏或格式不正确。') from None
        return bytes(output)

    def finish(self):
        if self.prefix or self.decoder is None or not self.decoder.eof:
            raise RetrievalError('invalid_compressed_content', '公开来源压缩数据不完整。')

class PublicFetcher:

    def __init__(self, *, resolver=None, connection_factory=None, registry=None):
        self.resolver = resolver or socket.getaddrinfo
        self.connection_factory = connection_factory or _connection
        self.registry = registry if registry is not None else load_sources()
        self.request_count, self.received_bytes = (0, 0)
        self.decoded_bytes = 0
        self.proxy_compatible_hosts = set()

    def _account_decoded(self, size):
        self.decoded_bytes += size

    def get(self, url, *, max_bytes, deadline):
        current = public_url(url)
        original_host = urlsplit(current).hostname
        redirects = []
        for hop in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetrievalError('research_timeout', '本次公开资料检索已到时间上限。')
            if self.request_count >= 9:
                raise RetrievalError('request_limit', '本次公开资料请求数量已达上限。')
            part = urlsplit(current)
            allowed_host = any((part.hostname in row['hosts'] for row in self.registry))
            addresses = _public_addresses(part.hostname, self.resolver, deadline, allow_fake_ip=allowed_host)
            if addresses is None:
                self.proxy_compatible_hosts.add(part.hostname)
            connection = self.connection_factory(part.hostname, None if addresses is None else addresses[0], max(0.01, min(5, deadline - time.monotonic())))
            try:
                self.request_count += 1
                connection.request('GET', (part.path or '/') + ('?' + part.query if part.query else ''), headers={'Host': part.netloc, 'User-Agent': 'MaaUma-TeamResearch/1', 'Accept-Encoding': 'identity', 'Accept': 'text/html,application/xhtml+xml,application/rss+xml,application/xml,text/plain;q=0.8'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    if hop >= 2:
                        raise RetrievalError('redirect_limit', '攻略页面跳转过多，请提供最终 HTTPS 链接。')
                    destination = public_url(urljoin(current, response.getheader('Location') or ''))
                    destination_host = urlsplit(destination).hostname
                    known = source_profile(destination, self.registry)['priority'] > 0
                    if destination_host.removeprefix('www.') != original_host.removeprefix('www.') and (not known):
                        raise RetrievalError('redirect_source_changed', '页面跳到了未登记的另一来源，请直接提供目标链接。')
                    redirects.append({'url': current, 'status': response.status, 'destination': destination})
                    current = destination
                    continue
                if not 200 <= response.status < 300:
                    raise RetrievalError('source_http_error', '公开来源拒绝访问或暂不可用。')
                encoding = response.getheader('Content-Encoding', 'identity').strip().casefold()
                if encoding == 'x-gzip':
                    encoding = 'gzip'
                if encoding not in ('', 'identity', 'gzip', 'deflate'):
                    raise RetrievalError('unsupported_encoding', '来源未返回可有界读取的普通文本。')
                decoder = _BoundedDecoder(encoding) if encoding in ('gzip', 'deflate') else None
                content_type = response.getheader('Content-Type', '').casefold()
                if not any((value in content_type for value in ('text/', 'application/xhtml+xml', 'application/rss+xml', 'application/xml', 'application/json'))):
                    raise RetrievalError('unsupported_content', '仅提取公开网页文本，不下载图像、视频或其他二进制文件。')
                length = response.getheader('Content-Length')
                if length is not None and (not length.isdigit() or int(length) > max_bytes):
                    raise RetrievalError('source_too_large', '公开来源内容过大，本次未读取正文。')
                data = bytearray()
                wire_bytes = 0
                read = getattr(response, 'read1', response.read)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RetrievalError('research_timeout', '本次公开资料检索已到时间上限。')
                    if getattr(connection, 'sock', None) is not None:
                        connection.sock.settimeout(max(0.01, remaining))
                    block = read(min(65536, max_bytes + 1 - wire_bytes, TOTAL_BYTES + 1 - self.received_bytes))
                    if not block:
                        break
                    wire_bytes += len(block)
                    self.received_bytes += len(block)
                    if wire_bytes > max_bytes or self.received_bytes > TOTAL_BYTES:
                        raise RetrievalError('source_too_large', '本次公开资料读取字节已达上限。')
                    decoded = decoder.feed(block, min(max_bytes - len(data), TOTAL_BYTES - self.decoded_bytes), deadline, self._account_decoded) if decoder else block
                    data.extend(decoded)
                    if not decoder:
                        self.decoded_bytes += len(decoded)
                    if len(data) > max_bytes or self.decoded_bytes > TOTAL_BYTES:
                        raise RetrievalError('source_too_large', '本次公开资料解压后文本字节已达上限。')
                if decoder:
                    decoder.finish()
                return {'url': current, 'body': bytes(data), 'content_type': content_type, 'redirects': redirects}
            except RetrievalError:
                raise
            except (OSError, http.client.HTTPException, ValueError):
                raise RetrievalError('source_connection_failed', '公开来源连接失败，本次没有取得正文。') from None
            finally:
                connection.close()

def _public_description(value):
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError('需求描述最多4000字。')
    text = re.sub(r'https://[^\s<>"\']+', '', value).strip()
    if re.search(r'(?i)账号|账户|密码|凭据|token|api.?key|\bsk-|://|[\\/@]|\d{6,}', text):
        raise RetrievalError('non_public_query', '补充描述含不适合公开检索的资料，请仅填写公开比赛、赛道或角色需求。')
    return re.sub(r'\s+', ' ', text)[:360]

def build_search_query(requirements, request_text=''):
    req = normalize_requirements(requirements)
    event = req['event_name'] or ''
    if re.search('(?i)账号|账户|密码|凭据|token|api.?key|\\bsk-|://|[\\\\/@]|\\d{6,}', event) or len(event) > 80:
        raise RetrievalError('non_public_query', '赛事名称含不适合公开检索的资料，请只填写公开比赛名称。')
    description = _public_description(request_text)
    words = ['闪耀优俊少女 国服']
    if req['event_mode']:
        words.append({'champions_meeting': '大赛 群英联赛', 'loh': 'LOH 英杰集结战', 'team_arena': '竞技场', 'custom': '自定赛道'}.get(req['event_mode'], ''))
    if event:
        words.append(event)
    if req['edition']['year']:
        words.append(str(req['edition']['year']) + '年')
    if req['edition']['month']:
        words.append(str(req['edition']['month']) + '月')
    if req['edition']['index']:
        words.append('第' + str(req['edition']['index']) + '届')
    for key, mapping in (('venue', {v: k for k, v in VENUES.items()}), ('surface', {'turf': '草地', 'dirt': '泥地'}), ('distance_category', {'sprint': '短距离', 'mile': '英里', 'middle': '中距离', 'long': '长距离'})):
        if req[key]:
            words.append(mapping[req[key]])
    if req['distance_m']:
        words.append(str(req['distance_m']) + '米')
    if req['direction']:
        words.append({'left': '逆时针', 'right': '顺时针', 'straight': '直线'}[req['direction']])
    for key, mapping in (('season', {'spring': '春季', 'summer': '夏季', 'autumn': '秋季', 'winter': '冬季'}),
                         ('weather', {'sunny': '晴天', 'cloudy': '阴天', 'rainy': '雨天', 'snowy': '雪天'}),
                         ('track_condition', {'good': '良好', 'yielding': '稍重', 'soft': '重场', 'heavy': '不良'})):
        if req[key]:
            words.append(mapping[req[key]])
    style_names = {'nige': '领跑', 'senko': '跟前', 'sashi': '居中', 'oikomi': '后追'}
    for style in dict.fromkeys([*(req['styles']['allowed'] or []), *req['styles']['preferred'], *req['styles']['counts']]):
        words.append(('不安排' if req['styles']['counts'].get(style) == 0 else '') + style_names[style])
    words.extend('不安排' + style_names[style] for style in req['styles']['excluded'])
    if description:
        words.append(description)
    if len(words) == 1 and req['team_size'] is not None:
        words.append(str(req['team_size']) + '匹阵容')
    if len(words) == 1:
        raise RetrievalError('no_search_terms', '请先写一句比赛、赛道或角色需求，或粘贴公开HTTPS攻略链接。')
    words.append('攻略')
    return ' '.join(words)

def _decode(response):
    charset = re.search('charset\\s*=\\s*["\\\']?([a-z0-9_-]+)', response['content_type'])
    encoding = charset.group(1) if charset else 'utf8'
    if encoding not in {'utf-8', 'utf8', 'gb18030', 'gbk', 'big5', 'shift_jis', 'euc-jp'}:
        encoding = 'utf8'
    return response['body'].decode(encoding, errors='replace')

def parse_rss(body):
    if len(body) > RSS_BYTES or b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise RetrievalError('rss_invalid', '公开搜索未返回受支持的结果格式。')
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise RetrievalError('rss_invalid', '公开搜索没有返回有效 RSS。') from None
    rows, seen = ([], set())
    for item in root.findall('.//item')[:15]:
        try:
            url = public_url(item.findtext('link', ''))
        except RetrievalError:
            continue
        if url in seen:
            continue
        seen.add(url)
        rows.append({'url': url, 'title': (item.findtext('title', '') or '')[:180], 'description': re.sub('<[^>]*>', ' ', item.findtext('description', '') or '')[:480]})
    return rows

def build_wiki_query(requirements, request_text=''):
    req = normalize_requirements(requirements)
    build_search_query(req, request_text)
    words = [req['event_name']] if req['event_name'] else []
    if req['venue']:
        words.append(next((name for name, value in VENUES.items() if value == req['venue'])))
    if req['surface']:
        words.append({'turf': '草地', 'dirt': '泥地'}[req['surface']])
    if req['distance_m']:
        words.append(str(req['distance_m']))
    elif req['distance_category']:
        words.append({'sprint': '短距离', 'mile': '英里', 'middle': '中距离', 'long': '长距离'}[req['distance_category']])
    description = _public_description(request_text)
    if description:
        words.append(description)
    if not words and req['event_mode']:
        words.append('群英联赛' if req['event_mode'] == 'champions_meeting' else '英杰集结战' if req['event_mode'] == 'loh' else '比赛阵容')
    return ' '.join(words) or '比赛阵容'

def parse_wiki_search(body):
    if len(body) > RSS_BYTES:
        raise RetrievalError('wiki_search_invalid', '本地登记 Wiki 搜索响应过大。')
    try:
        value = json.loads(body.decode('utf8'))
        rows = value['query']['search']
        if not isinstance(rows, list):
            raise ValueError()
        result = []
        for row in rows[:5]:
            title = row.get('title')
            if not isinstance(title, str) or not title.strip() or len(title) > 240:
                continue
            result.append({'url': 'https://wiki.biligame.com/umamusume/' + quote(title.replace(' ', '_'), safe='/:'), 'title': title[:180], 'description': re.sub('<[^>]*>', ' ', str(row.get('snippet') or ''))[:480]})
        return result
    except (ValueError, KeyError, TypeError, UnicodeError, AttributeError):
        raise RetrievalError('wiki_search_invalid', '本地登记 Wiki 搜索未返回可用 JSON，可能暂时需要浏览器验证。') from None

class ArticleText(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title_parts, self.skip = ([], [], 0)
        self.in_title, self.published, self.description = (False, None, '')

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.skip += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'meta':
            key = (attrs.get('property') or attrs.get('name') or '').casefold()
            if key in ('article:published_time', 'datepublished', 'pubdate', 'publishdate', 'date'):
                self.published = self.published or attrs.get('content')
            if key in ('description', 'og:description') and (not self.description):
                self.description = attrs.get('content', '')
        if tag == 'time' and (not self.published):
            self.published = attrs.get('datetime')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg') and self.skip:
            self.skip -= 1
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title_parts.append(data)
        self.parts.append(data)

    def result(self):
        title = ' '.join(self.title_parts).strip()
        text = re.sub('\\s+', ' ', ' '.join(self.parts)).strip()
        return (title, text or self.description, self.published)

def _shared_guide_urls(guide_urls, request_text):
    """GUI tokens can contain a share title, a URL, or both; count only URLs."""
    candidates = []
    for shared in guide_urls:
        if not isinstance(shared, str):
            raise RetrievalError('invalid_public_url', '攻略分享内容应为文字或公网 HTTPS 链接。')
        # Keep explicit non-HTTPS links for public_url to reject, rather than
        # silently turning an invalid pasted link into a search request.
        shared = shared.strip()
        if re.fullmatch(r'[a-z][a-z0-9+.-]*://\S+', shared, re.IGNORECASE):
            candidates.append(shared)
        else:
            candidates.extend(re.findall(r'[a-z][a-z0-9+.-]*://[^\s<>"]+', shared, re.IGNORECASE))
    candidates.extend(re.findall(r'https://[^\s<>"\']+', request_text, re.IGNORECASE))
    if guide_urls and not candidates:
        raise RetrievalError('invalid_public_url', '未从攻略分享内容中找到公网 HTTPS 链接，请检查粘贴内容。')
    urls = list(dict.fromkeys(public_url(url.rstrip('，。；！？、）】》」』')) for url in candidates))
    if len(urls) > MAX_PAGES:
        raise ValueError('每次最多3条公开HTTPS攻略链接。')
    return urls


def retrieve_guides(requirements, guide_urls=(), allow_network=False, *, request_text='', fetcher=None, race_catalog=None, skill_reference=None, registry=None):
    req = normalize_requirements(requirements)
    assessed = assess_requirements(req)
    if type(allow_network) is not bool or not isinstance(guide_urls, (list, tuple)) or any(not isinstance(value, str) for value in guide_urls):
        raise ValueError('公开检索开关或攻略分享内容格式无效。')
    if skill_reference is None:
        from core.parent_skill_reference import SkillReference
        skill_reference = SkillReference.load()
    result = {'status': 'offline', 'facts': local_facts(req, race_catalog=race_catalog, skill_reference=skill_reference), 'guides': [], 'queries': [], 'missing_data': [], 'errors': [], 'network_requested': allow_network, 'paid_api_used': False, 'retrieved_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    result['missing_data'].extend(assessed['conflicts'] + assessed['clarification_questions'] + assessed['notes'])
    if not isinstance(request_text, str) or len(request_text) > 4000:
        raise ValueError('需求描述最多4000字。')
    if not allow_network:
        result['missing_data'].append('当前仅使用本地事实，尚未取得公开攻略；本地赛历与技能目录不等于赛事攻略。')
        return result
    urls = _shared_guide_urls(guide_urls, request_text)
    registry = registry if registry is not None else load_sources()
    fetcher = fetcher or PublicFetcher(registry=registry)
    deadline = time.monotonic() + TOTAL_SECONDS
    entries = [{'url': url, 'title': '', 'description': ''} for url in dict.fromkeys(urls)]
    if not entries:
        try:
            query = build_search_query(req, request_text)
        except RetrievalError as error:
            if error.code != 'no_search_terms':
                raise
            result.update(status='needs_clarification')
            result['missing_data'].append(error.message)
            return result
        try:
            result['queries'].append(query)
            rss = fetcher.get('https://www.bing.com/search?' + urlencode({'format': 'rss', 'q': query}), max_bytes=RSS_BYTES, deadline=deadline)
            entries = parse_rss(rss['body'])
            entries = [row for row in entries if source_profile(row['url'], registry)['priority'] >= 20 and (not source_profile(row['url'], registry).get('metadata_only')) or re.search('(?i)闪耀优俊少女|赛马娘|賽馬娘|ウマ娘|uma[\\s_-]*musume', row['title'] + ' ' + row['description'])]
            entries.sort(key=lambda row: -source_profile(row['url'], registry)['priority'])
            entries = entries[:2]
            if not entries:
                raise RetrievalError('no_relevant_search_results', '公开搜索可以连接，但本次没有命中与赛马娘有关的攻略来源。')
        except RetrievalError as error:
            result['errors'].append({'code': error.code, 'message': error.message})
            try:
                query = build_wiki_query(req, request_text)
                result['queries'].append('BWIKI: ' + query)
                wiki = fetcher.get('https://wiki.biligame.com/umamusume/api.php?' + urlencode({'action': 'query', 'list': 'search', 'format': 'json', 'srsearch': query, 'srlimit': 5, 'srnamespace': 0}), max_bytes=RSS_BYTES, deadline=deadline)
                entries = parse_wiki_search(wiki['body'])[:2]
                if not entries:
                    raise RetrievalError('no_wiki_results', '登记 Wiki 来源没有命中本次公开比赛条件。')
            except RetrievalError as fallback_error:
                result['errors'].append({'code': fallback_error.code, 'message': fallback_error.message})
                result['missing_data'].append('公开搜索与登记 Wiki 暂未取得可用来源，请提供明确的公网 HTTPS 攻略链接；本次不把模型记忆当作检索结果。')
                return result
    for row in entries[:MAX_PAGES]:
        try:
            response = fetcher.get(row['url'], max_bytes=ARTICLE_BYTES, deadline=deadline)
            parser = ArticleText()
            parser.feed(_decode(response))
            title, text, published = parser.result()
            if not text or re.search('(?i)^\\s*(?:just a moment|access denied|forbidden)', title):
                raise RetrievalError('body_unavailable', '页面正文暂不可读取，可能需要浏览器验证。')
            evidence = article_evidence(response['url'], title or row['title'], text, req, published=published, registry=registry, retrieved_at=result['retrieved_at'])
            evidence['content_scope'] = 'public_page_text'
            if source_profile(response['url'], registry).get('metadata_only'):
                evidence['content_scope'] = 'video_page_metadata_only'
            result['guides'].append(evidence)
        except RetrievalError as error:
            result['errors'].append({'code': error.code, 'message': error.message})
            if row['title'] or row['description']:
                evidence = article_evidence(row['url'], row['title'], row['description'], req, registry=registry, retrieved_at=result['retrieved_at'])
                evidence['content_scope'] = 'search_metadata_only'
                if evidence['applicability']['status'] != 'out_of_scope':
                    evidence['applicability']['status'] = 'reference_only'
                evidence['applicability']['reasons'].append('仅取得公开搜索摘要，未读取页面正文。')
                result['guides'].append(evidence)
    result['status'] = 'partial' if result['guides'] and result['errors'] else 'online' if result['guides'] else 'offline'
    if not result['guides']:
        result['missing_data'].append('没有取得可引用攻略，请提供明确链接或先使用标明不确定性的本地参考方案。')
    return result
