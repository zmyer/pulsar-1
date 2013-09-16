'''Pulsar has an :class:`HttpClient` class for asynchronous HTTP requests::

    >>> from pulsar.apps import http
    >>> client = http.HttpClient()
    >>> response = http.get('http://www.bbc.co.uk')

.. contents::
    :local:

Making requests
=================
Pulsar HTTP client has no dependencies and an API similar to requests_::
    
    from pulsar.apps import http
    client = http.HttpClient()
    resp = client.get('https://github.com/timeline.json')
    
``resp`` is a :class:`HttpResponse` object which contains all the information
about the request and, once finished, the result.

The ``resp`` is finished once the ``on_finished`` attribute
(a :class:`pulsar.Deferred`) is fired. In a :ref:`coroutine <coroutine>` one
can obtained a full response by yielding ``on_finished``::

    resp = yield client.get('https://github.com/timeline.json').on_finished

Cookie support
================

Cookies are handled by the client by storing cookies received with responses.
To disable cookie one can pass ``store_cookies=False`` during
:class:`HttpClient` initialisation.

.. _http-authentication:

Authentication
======================

Headers authentication, either ``basic`` or ``digest``, can be added to a
client by invoking

* :meth:`HttpClient.add_basic_authentication` method
* :meth:`HttpClient.add_digest_authentication` method

In either case the authentication is handled by adding additional headers
to your requests.

TLS/SSL
=================
Supported out of the box::

    client = HttpClient()
    client.get('https://github.com/timeline.json')


you can include certificate file and key too, either
to a :class:`HttpClient` or to a specific request:

    client = HttpClient(certkey='public.key')
    res1 = client.get('https://github.com/timeline.json')
    res2 = client.get('https://github.com/timeline.json', certkey='another.key')
    
.. _http-streaming:

Streaming & WebSocket
=========================

This is an event-driven client, therefore streaming is supported as a
consequence. The ``on_finished`` callback is only fired when the server has
finished with the response.
Check the :ref:`proxy server <tutorials-proxy-server>` example for an
application using the :class:`HttpClient` streaming capabilities.


Redirects & Decompression
=============================

Synchronous Mode
=====================

* Thread safe
* Can be used in :ref:`synchronous mode <tutorials-synchronous>`

Events
==============
Events are used to customise the behaviour of the Http client when certain
headers or responses occurs. There are three
:ref:`one time events <one-time-event>` associated with an
:class:`HttpResponse` object:

* ``pre_request``, fired before the request is sent to the server. Callbacks
  receive the *response* and *request* arguments.
* ``on_headers``, fired when response headers are available. Callbacks
  receive the *response* and response *headers* arguments.
* ``post_request``, fired when the response is done. Callbacks
  receive the *response* and another argument, usually *None*.

Adding event handlers can be done at client level::

    def myheader_handler(response, request):
        pass

    client.bind_event('on_headers', myheader_handler)

or at request level::

    response = client.get(..., on_headers=myheader_handler)

By default, the :class:`HttpClient` has one ``pre_request`` callback for
handling `HTTP tunneling`_, three ``on_headers`` callbacks for
handling *100 Continue*, *websocket upgrade* and *cookies*, and one
``post_request`` callback for handling redirects.


API
==========

The main class here is the :class:`HttpClient` which is a subclass of
:class:`pulsar.Client`.
You can use the client as a global singletone::


    >>> requests = HttpClient()

and somewhere else

    >>> resp = requests.post('http://bla.foo', body=...)

the same way requests_ works, otherwise use it where you need it.


HTTP Client
~~~~~~~~~~~~~~~~~~

.. autoclass:: HttpClient
   :members:
   :member-order: bysource
   
   
HTTP Request
~~~~~~~~~~~~~~~~~~

.. autoclass:: HttpRequest
   :members:
   :member-order: bysource
   
HTTP Response
~~~~~~~~~~~~~~~~~~

.. autoclass:: HttpResponse
   :members:
   :member-order: bysource


.. _requests: http://docs.python-requests.org/
.. _`uri scheme`: http://en.wikipedia.org/wiki/URI_scheme
.. _`HTTP tunneling`: http://en.wikipedia.org/wiki/HTTP_tunnel
'''
import os
import platform
import json
from copy import copy
from collections import namedtuple
from base64 import b64encode

import pulsar
from pulsar import is_failure
from pulsar.utils.pep import native_str, is_string, to_bytes, ispy33
from pulsar.utils.structures import mapping_iterator
from pulsar.utils.websocket import SUPPORTED_VERSIONS
from pulsar.utils.internet import CERT_NONE, SSLContext
from pulsar.utils.httpurl import (urlparse, parse_qsl,
                                  http_parser, ENCODE_URL_METHODS,
                                  encode_multipart_formdata, urlencode,
                                  Headers, urllibr, get_environ_proxies,
                                  choose_boundary, urlunparse, request_host,
                                  responses, is_succesful, HTTPError, URLError,
                                  get_hostport, CookieJar,
                                  cookiejar_from_dict)

from .plugins import (handle_cookies, handle_100, handle_101, handle_redirect,
                      Tunneling, TooManyRedirects)
                      
from .auth import Auth, HTTPBasicAuth, HTTPDigestAuth


scheme_host = namedtuple('scheme_host', 'scheme netloc')
tls_schemes = ('https', 'wss') 


class HttpRequest(pulsar.Request):
    '''An :class:`HttpClient` request for an HTTP resource.
    
    .. attribute:: method

        The request method
        
    .. attribute:: version

        HTTP version for this request, usually ``HTTP/1.1``

    .. attribute:: history
    
        List of past :class:`HttpResponse` (collected during redirects).
        
    .. attribute:: wait_continue

        if ``True``, the :class:`HttpRequest` includes the
        ``Expect: 100-Continue`` header.
        
    '''
    full_url = None
    _proxy = None
    _ssl = None
    _tunnel = None
    _tunnel_headers = None
    def __init__(self, client, url, method, inp_params, headers=None,
                 data=None, files=None, timeout=None, history=None,
                 charset=None, encode_multipart=True, multipart_boundary=None,
                 source_address=None, allow_redirects=False, max_redirects=10,
                 decompress=True, version=None, wait_continue=False,
                 websocket_handler=None, cookies=None, **ignored):
        self.client = client
        self.inp_params = inp_params
        self.unredirected_headers = Headers(kind='client')
        self.timeout = timeout
        self.method = method.upper()
        self._scheme, self._netloc, self.path, self.params,\
        self.query, self.fragment = urlparse(url)
        if not self._netloc:
            if self.method == 'CONNECT':
                # Using this request to create a tunnel (SSL tunneling)
                self._netloc = self.path
                self.path = ''
        self.set_proxy(None)
        self.history = history
        self.wait_continue = wait_continue
        self.max_redirects = max_redirects
        self.allow_redirects = allow_redirects
        self.charset = charset or 'utf-8'
        self.version = version
        self.decompress = decompress
        self.encode_multipart = encode_multipart 
        self.multipart_boundary = multipart_boundary
        self.websocket_handler = websocket_handler
        self.data = data if data is not None else {}
        self.files = files
        self.source_address = source_address
        self.new_parser()
        self.headers = client.get_headers(self, headers)
        if self._scheme in tls_schemes:
            self._ssl = client.ssl_context(**ignored)
        client.set_proxy(self)
        if client.cookies:
            client.cookies.add_cookie_header(self)
        if cookies:
            cookiejar_from_dict(cookies).add_cookie_header(self)
    
    @property
    def address(self):
        '''``(host, port)`` tuple of the HTTP resource'''
        return (self.host, int(self.port))
    
    @property
    def ssl(self):
        '''Context for TLS connections.'''
        return False if self._proxy else self._ssl

    @property
    def key(self):
        return (self.scheme, self.address, self.timeout)
    
    @property
    def proxy(self):
        '''Proxy server for this request.'''
        return self._proxy
    
    @property
    def tunnel_headers(self):
        '''Headers for HTTP CONNECT Tunneling.'''
        if self._tunnel_headers is None:
            self._tunnel_headers = Headers(kind='client')
        return self._tunnel_headers
    
    @property
    def unverifiable(self):
        '''Unverifiable when a redirect.
        
        It is a redirect when :attr:`history` has past requests.
        '''
        return bool(self.history)
    
    @property
    def origin_req_host(self):
        if self.history:
            return self.history[0].current_request.origin_req_host
        else:
            return request_host(self)
    
    @property
    def netloc(self):
        if self._proxy:
            return self._proxy.netloc
        else:
            return self._netloc
        
    def __repr__(self):
        return self.first_line()
    
    def __str__(self):
        return self.__repr__()
    
    def get_full_url(self):
        '''The full url for this request.'''
        self.full_url = urlunparse((self._scheme, self._netloc, self.path,
                                    self.params, self.query, self.fragment))
        return self.full_url
        
    def first_line(self):
        url = self.get_full_url()
        if not self._proxy:
            url = urlunparse(('', '', self.path or '/', self.params,
                              self.query, self.fragment))
        return '%s %s %s' % (self.method, url, self.version)
    
    def new_parser(self):
        self.parser = http_parser(kind=1, decompress=self.decompress)
        
    def set_proxy(self, scheme, *host):
        if not host and scheme is None:
            self._proxy = None
            self._set_hostport(self._scheme, self._netloc)
            self.scheme = self._scheme
        else:
            le = 2 + len(host)
            if not le == 3:
                raise TypeError(
                    'set_proxy() takes exactly three arguments (%s given)' % le)
            self._proxy = scheme_host(scheme, host[0])
            if not self._ssl:
                self.scheme = scheme
            self._set_hostport(scheme, host[0])
    
    def _set_hostport(self, scheme, host):
        self.host, self.port = get_hostport(scheme, host)
        
    def encode(self):
        '''The bytes representation of this :class:`HttpRequest`.

        Called by :class:`HttpResponse` when it needs to encode this
        :class:`HttpRequest` before sending it to the HTTP resourse.
        '''
        if self.method == 'CONNECT':    # this is SSL tunneling
            return b''
            # Call body before fist_line in case the query is changes.
        self.body = body = self.encode_body()
        first_line = self.first_line()
        if body:
            self.headers['content-length'] = str(len(body))
            if self.wait_continue:
                self.headers['expect'] = '100-continue'
                body = None
        headers = self.headers
        if self.unredirected_headers:
            headers = self.unredirected_headers.copy()
            headers.update(self.headers)
        buffer = [first_line.encode('ascii'), b'\r\n',  bytes(headers)]
        if body:
            buffer.append(body)
        return b''.join(buffer)
        
    def encode_body(self):
        '''Encode body or url if the :attr:`method` does not have body.
        
        Called by :meth:`encode`.'''
        body = None
        if self.method in ENCODE_URL_METHODS:
            self.files = None
            self._encode_url(self.data)
        elif isinstance(self.data, bytes):
            body = self.data
        elif is_string(self.data):
            body = to_bytes(self.data, self.charset)
        elif self.data:
            content_type = self.headers.get('content-type')
            # No content type given
            if not content_type:
                content_type = 'application/x-www-form-urlencoded'
                if self.encode_multipart:
                    body, content_type = encode_multipart_formdata(
                                            self.data,
                                            boundary=self.multipart_boundary,
                                            charset=self.charset)
                else:
                    body = urlencode(self.data).encode(self.charset)
                self.headers['Content-Type'] = content_type
            elif content_type == 'application/json':
                body = json.dumps(self.data).encode(self.charset)
            else:
                body = json.dumps(self.data).encode(self.charset)
        return body
    
    def has_header(self, header_name):
        return (header_name in self.headers or
                header_name in self.unredirected_headers)

    def get_header(self, header_name, default=None):
        return self.headers.get(header_name,
            self.unredirected_headers.get(header_name, default))

    def remove_header(self, header_name):
        self.headers.pop(header_name, None)
        self.unredirected_headers.pop(header_name, None)
    
    def add_unredirected_header(self, header_name, header_value):
        self.unredirected_headers[header_name] = header_value
        
    def _encode_url(self, body):
        query = self.query
        if body:
            body = native_str(body)
            if isinstance(body, str):
                body = parse_qsl(body)
            else:
                body = mapping_iterator(body)
            query = parse_qsl(query)
            query.extend(body)
            self.data = query
            query = urlencode(query)
        self.query = query

    if not ispy33:  #pragma     nocover
        # Provide support for python < 3.3
        def is_unverifiable(self):
            return self.unverifiable

        def get_origin_req_host(self):
            return self.origin_req_host
        
        
class HttpResponse(pulsar.ProtocolConsumer):
    '''A :class:`pulsar.ProtocolConsumer` for the HTTP client protocol.

    Initialised by a call to the :class:`HttpClient.request` method.
    
    There are two events you can yield in a coroutine:
    
    .. attribute:: on_headers
    
        fired once the response headers are received.
        
    .. attribute:: on_finished
    
        Fired once the whole request has finished
        
    Public API:
    '''
    _tunnel_host = None
    _has_proxy = False
    _content = None
    _data_sent = None
    _history = None
    ONE_TIME_EVENTS = (pulsar.ProtocolConsumer.ONE_TIME_EVENTS + 
                        ('on_headers', 'on_message_complete'))
    
    @property
    def parser(self):
        if self._request:
            return self._request.parser
    
    def __str__(self):
        return self.status or '<None>'

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, self)

    @property
    def status_code(self):
        '''Numeric status code such as 200, 404 and so forth.
        
        Available once the :attr:`on_headers` has fired.'''
        if self.parser:
            return self.parser.get_status_code()
    
    @property
    def response(self):
        '''The description of the :attr:`status_code`.

        This is the second part of the status string.'''
        if self.status_code:
            return responses.get(self.status_code)
        
    @property
    def status(self):
        status_code = self.status_code
        if status_code:
            return '%s %s' % (status_code, responses.get(status_code))
        
    @property
    def url(self):
        '''The request full url.'''
        if self._request is not None:
            return self._request.full_url
    
    @property
    def history(self):
        return self._history
    
    @property
    def headers(self):
        if not hasattr(self, '_headers'):
            if self.parser and self.parser.is_headers_complete():
                self._headers = Headers(self.parser.get_headers())
        return getattr(self, '_headers', None)
    
    @property
    def is_error(self):
        if self.status_code:
            return not is_succesful(self.status_code)
        elif self.on_finished.done():
            return is_failure(self.on_finished.result)
        else:
            return False
    
    @property
    def on_headers(self):
        return self.event('on_headers')
    
    def recv_body(self):
        '''Flush the response body and return it.'''
        return self.parser.recv_body()
    
    def get_content(self):
        '''Retrieve the body without flushing'''
        b = self.parser.recv_body()
        if b or self._content is None:
            self._content = self._content + b if self._content else b
        return self._content
    
    def content_string(self, charset=None, errors=None):
        '''Decode content as a string.'''
        data = self.get_content()
        if data is not None:
            return data.decode(charset or 'utf-8', errors or 'strict')

    def content_json(self, charset=None, **kwargs):
        '''Decode content as a JSON object.'''
        return json.loads(self.content_string(charset), **kwargs)
    
    def raise_for_status(self):
        '''Raises stored :class:`HTTPError` or :class:`URLError`, if occured.
        '''
        if self.is_error:
            if self.status_code:
                raise HTTPError(self.url, self.status_code,
                                self.content_string(), self.headers, None)
            else:
                raise URLError(self.on_finished.result.error)
    
    def info(self):
        '''Required by python CookieJar.
        
        Return :attr:`headers`.'''
        return self.headers
    
    ############################################################################
    ##    PROTOCOL IMPLEMENTATION
    def start_request(self):
        self.transport.write(self._request.encode())
        
    def data_received(self, data):
        if self._request.parser.execute(data, len(data)) == len(data):
            if self._request.parser.is_headers_complete():
                if not self.event('on_headers').done():
                    self.fire_event('on_headers', callback=self._continue)
        else:
            raise pulsar.ProtocolError
        
    def close(self):
        if self.parser.is_message_complete():
            self.finished()
            if self.next_url:
                return self.new_request(self.next_url)
            return self
    
    def _continue(self, result):
        if not self.has_finished and self._request.parser.is_message_complete():
            self.finished()
        return result

class HttpClient(pulsar.Client):
    '''A :class:`pulsar.Client` for HTTP/HTTPS servers.

    As :class:`pulsar.Client` it handles
    a pool of asynchronous :class:`pulsar.Connection`.

    .. attribute:: headers

        Default headers for this :class:`HttpClient`.

        Default: :attr:`DEFAULT_HTTP_HEADERS`.

    .. attribute:: cookies

        Default cookies for this :class:`HttpClient`.

    .. attribute:: timeout

        Default timeout for the connecting sockets. If 0 it is an asynchronous
        client.

    .. attribute:: encode_multipart

        Flag indicating if body data is encoded using the ``multipart/form-data``
        encoding by default. It can be overwritten during a :meth:`request`.

        Default: ``True``

    .. attribute:: proxy_info

        Dictionary of proxy servers for this client.
        
    .. attribute:: DEFAULT_HTTP_HEADERS

        Default headers for this :class:`HttpClient`
        
    '''
    MANY_TIMES_EVENTS = pulsar.Client.MANY_TIMES_EVENTS + ('on_headers',)
    consumer_factory = HttpResponse
    allow_redirects = False
    max_redirects = 10
    '''Maximum number of redirects.

    It can be overwritten on :meth:`request`.'''
    client_version = 'Python-httpurl'
    '''String for the ``User-Agent`` header.'''
    version = 'HTTP/1.1' 
    '''Default HTTP request version for this :class:`HttpClient`.

    It can be overwritten on :meth:`request`.'''
    DEFAULT_HTTP_HEADERS = Headers([
            ('Connection', 'Keep-Alive'),
            ('Accept-Encoding', 'identity'),
            ('Accept-Encoding', 'deflate'),
            ('Accept-Encoding', 'compress'),
            ('Accept-Encoding', 'gzip')],
            kind='client')
    request_parameters = ('encode_multipart', 'max_redirects', 'decompress',
                          'allow_redirects', 'multipart_boundary', 'version',
                          'timeout', 'websocket_handler')
    # Default hosts not affected by proxy settings. This can be overwritten
    # by specifying the "no" key in the proxy_info dictionary
    no_proxy = set(('localhost', urllibr.localhost(), platform.node()))

    def setup(self, proxy_info=None, cache=None, headers=None,
              encode_multipart=True, multipart_boundary=None,
              keyfile=None, certfile=None, cert_reqs=CERT_NONE,
              ca_certs=None, cookies=None, store_cookies=True,
              max_redirects=10, decompress=True, version=None,
              websocket_handler=None):
        self.store_cookies = store_cookies
        self.max_redirects = max_redirects
        self.cookies = cookiejar_from_dict(cookies) 
        self.decompress = decompress
        self.version = version or self.version
        dheaders = self.DEFAULT_HTTP_HEADERS.copy()
        dheaders['user-agent'] = self.client_version
        if headers:
            dheaders.update(headers)
        self.headers = dheaders
        self.proxy_info = dict(proxy_info or ())
        if not self.proxy_info and self.trust_env:
            self.proxy_info = get_environ_proxies()
            if 'no' not in self.proxy_info:
                self.proxy_info['no'] = ','.join(self.no_proxy)
        self.encode_multipart = encode_multipart
        self.multipart_boundary = multipart_boundary or choose_boundary()
        self.websocket_handler = websocket_handler
        self.https_defaults = {'keyfile': keyfile,
                               'certfile': certfile,
                               'cert_reqs': cert_reqs,
                               'ca_certs': ca_certs}
        # Hooks Events
        self.bind_event('pre_request', Tunneling())
        self.bind_event('on_headers', handle_101)
        self.bind_event('on_headers', handle_100)
        self.bind_event('on_headers', handle_cookies)
        self.bind_event('post_request', handle_redirect)

    @property
    def websocket_key(self):
        if not hasattr(self, '_websocket_key'):
            self._websocket_key = native_str(b64encode(os.urandom(16)),
                                             'latin-1')
        return self._websocket_key
    
    def get(self, url, **kwargs):
        '''Sends a GET request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        kwargs.setdefault('allow_redirects', True)
        return self.request('GET', url, **kwargs)

    def options(self, url, **kwargs):
        '''Sends a OPTIONS request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        kwargs.setdefault('allow_redirects', True)
        return self.request('OPTIONS', url, **kwargs)

    def head(self, url, **kwargs):
        '''Sends a HEAD request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        return self.request('HEAD', url, **kwargs)

    def post(self, url, **kwargs):
        '''Sends a POST request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        return self.request('POST', url, **kwargs)

    def put(self, url, **kwargs):
        '''Sends a PUT request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        return self.request('PUT', url, **kwargs)

    def patch(self, url, **kwargs):
        '''Sends a PATCH request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        return self.request('PATCH', url, **kwargs)

    def delete(self, url, **kwargs):
        '''Sends a DELETE request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        return self.request('DELETE', url, **kwargs)
    
    def request(self, method, url, response=None, **params):
        '''Constructs and sends a request to a remote server.

        It returns an :class:`HttpResponse` object.

        :param method: request method for the :class:`HttpRequest`.
        :param url: URL for the :class:`HttpRequest`.
        :parameter response: optional pre-existing :class:`HttpResponse` which
            starts a new request (for redirects, digest authentication and
            so forth).
        :param params: optional parameters for the :class:`HttpRequest`
            initialisation.

        :rtype: a :class:`HttpResponse` object.
        '''
        request = self._build_request(method, url, response, params)
        return self.response(request, response)
    
    def again(self, response, method=None, url=None, params=None,
              history=False, request=None):
        '''Create a new request from ``response``.
        
        The input ``response`` must be done.
        '''
        assert response.has_finished, 'response has not finished'
        new_response = self.build_consumer()
        new_response.chain_event(response, 'post_request')
        if history:
            new_response._history = []
            new_response._history.extend(response._history or ())
            new_response._history.append(response)
        #
        if not request:
            request = response.request
            if params is None:
                params = request.inp_params.copy()
            if not method:
                method = request.method
            if not url:
                url = request.full_url
            request = self._build_request(method, url, new_response, params)
        #
        return self.response(request, new_response)
        
    def add_basic_authentication(self, username, password):
        '''Add a :class:`HTTPBasicAuth` handler to the ``pre_requests`` hook.
        '''
        self.bind_event('pre_request', HTTPBasicAuth(username, password))
        
    def add_digest_authentication(self, username, password):
        '''Add a :class:`HTTPDigestAuth` handler to the ``pre_requests`` hook.
        '''
        self.bind_event('pre_request', HTTPDigestAuth(username, password))

    def add_oauth2(self, client_id, client_secret):
        self.bind_event('pre_request', OAuth2(client_id, client_secret))

    #    INTERNALS
    
    def _build_request(self, method, url, response, params):
        nparams = self.update_parameters(self.request_parameters, params)
        if response:
            nparams['history'] = response.history
        return HttpRequest(self, url, method, params, **nparams)

    def get_headers(self, request, headers):
        #Returns a :class:`Header` obtained from combining
        #:attr:`headers` with *headers*. Can handle websocket requests.
        if request.scheme in ('ws','wss'):
            d = Headers((('Connection', 'Upgrade'),
                         ('Upgrade', 'websocket'),
                         ('Sec-WebSocket-Version', str(max(SUPPORTED_VERSIONS))),
                         ('Sec-WebSocket-Key', self.websocket_key),
                         ('user-agent', self.client_version)),
                         kind='client')
        else:
            d = self.headers.copy()
        if headers:
            d.update(headers)
        return d
    
    def ssl_context(self, **kwargs):
        params = self.https_defaults.copy()
        for name in kwargs:
            if name in params:
                params[name] = kwargs[name]
        return SSLContext(**params)

    def set_proxy(self, request):
        if request.scheme in self.proxy_info:
            hostonly = request.host
            no_proxy = [n for n in self.proxy_info.get('no','').split(',') if n]
            if not any(map(hostonly.endswith, no_proxy)):
                url = self.proxy_info[request.scheme]
                p = urlparse(url)
                if not p.scheme:
                    raise ValueError('Could not understand proxy %s' % url)
                request.set_proxy(p.scheme, p.netloc)
                
    def can_reuse_connection(self, connection, response):
        # Reuse connection only if the headers has Connection keep-alive
        if response and response.headers:
            return response.headers.has('connection', 'keep-alive')
        return False
    
