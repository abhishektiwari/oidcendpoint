import logging
# noinspection PyCompatibility
from urllib.parse import urlparse

from oidcmsg.exception import MissingRequiredAttribute
from oidcmsg.exception import MissingRequiredValue
from oidcmsg.message import Message
from oidcmsg.oauth2 import ResponseMessage

from oidcendpoint import sanitize
from oidcendpoint.client_authn import UnknownOrNoAuthnMethod
from oidcendpoint.client_authn import verify_client
from oidcendpoint.exception import UnAuthorizedClient
from oidcendpoint.util import OAUTH2_NOCACHE_HEADERS

__author__ = 'Roland Hedberg'

logger = logging.getLogger(__name__)

"""
method call structure for Endpoints:

parse_request
    - client_authentication (*)
    - post_parse_request (*)
    
process_request

do_response    
    - response_info
        - construct 
            - pre_construct (*)
            - _parse_args
            - post_construct (*)
    - update_http_args
"""


def set_content_type(headers, content_type):
    if ('Content-type', content_type) in headers:
        return headers

    _headers = [h for h in headers if h[0] != 'Content-type']
    _headers.append(('Content-type', content_type))
    return _headers


class Endpoint(object):
    request_cls = Message
    response_cls = Message
    error_cls = ResponseMessage
    endpoint_name = ''
    endpoint_path = ''
    request_format = 'urlencoded'
    request_placement = 'query'
    response_format = 'json'
    response_placement = 'body'
    client_auth_method = ''

    def __init__(self, keyjar, **kwargs):
        self.keyjar = keyjar
        self.pre_construct = []
        self.post_construct = []
        self.post_parse_request = []
        self.kwargs = kwargs

    def parse_request(self, endpoint_context, request, auth=None, **kwargs):
        """

        :param request:
        :param endpoint_context:
        :param auth:
        :param kwargs:
        :return:
        """
        logger.debug("- {} -".format(self.endpoint_name))
        logger.info("Request: %s" % sanitize(request))

        if request:
            if isinstance(request, dict):
                req = self.request_cls(**request)
            else:
                if self.request_format == 'jwt':
                    req = self.request_cls().deserialize(
                        request, "jwt", keyjar=endpoint_context.keyjar,
                        verify=endpoint_context.verify_ssl, **kwargs)
                elif self.request_format == 'url':
                    parts = urlparse(request)
                    scheme, netloc, path, params, query, fragment = parts[:6]
                    req = self.request_cls().deserialize(query, 'urlencoded')
                else:
                    req = self.request_cls().deserialize(request,
                                                         self.request_format)
        else:
            req = self.request_cls()

        # Verify that the client is allowed to do this
        _client_id = ''
        try:
            auth_info = self.client_authentication(endpoint_context, req, auth,
                                                   **kwargs)
        except UnknownOrNoAuthnMethod:
            if not self.client_auth_method:
                pass
            else:
                raise UnAuthorizedClient()
        else:
            if 'client_id' in auth_info:
                req['client_id'] = auth_info['client_id']
                _client_id = auth_info['client_id']
            else:
                try:
                    _client_id = req['client_id']
                except KeyError:
                    pass

        try:
            keyjar = self.keyjar
        except AttributeError:
            keyjar = ""

        # verify that the request message is correct
        try:
            req.verify(keyjar=keyjar, opponent_id=_client_id)
        except (MissingRequiredAttribute, ValueError,
                MissingRequiredValue) as err:
            return self.error_cls(error="invalid_request",
                                  error_description="%s" % err)

        logger.info("Parsed and verified request: %s" % sanitize(req))
        if endpoint_context.events:
            endpoint_context.events.store('Protocol request', request)

        # Do any endpoint specific parsing
        self.do_post_parse_request(endpoint_context, req, _client_id, **kwargs)
        return req

    def client_authentication(self, endpoint_context, request, auth=None,
                              **kwargs):
        """

        :param endpoint_context: A
        :py:class:`oidcendpoint.endpoint_context.SrvInfo` instance
        :param request: Parsed request, a self.request_cls class instance
        :param authn: Authorization info
        :return: client_id or raise and exception
        """

        return verify_client(endpoint_context, request, auth)

    def do_post_parse_request(self, endpoint_context, request, client_id='',
                              **kwargs):
        for meth in self.post_parse_request:
            request = meth(endpoint_context, request, client_id, **kwargs)
        return request

    def do_pre_construct(self, endpoint_context, response_args, request,
                         **kwargs):
        for meth in self.pre_construct:
            response_args = meth(endpoint_context, response_args, request,
                                 **kwargs)

        return response_args

    def do_post_construct(self, endpoint_context, response_args, request,
                          **kwargs):
        for meth in self.post_construct:
            response_args = meth(endpoint_context, response_args, request,
                                 **kwargs)

        return response_args

    def process_request(self, endpoint_context, request=None):
        """

        :param endpoint_context:
        :py:class:`oidcendpoint.endpoint_context.SrvInfo` instance
        :param request: The request, can be in a number of formats
        :return: Arguments for the do_response method
        """
        return {}

    def construct(self, endpoint_context, response_args, request, **kwargs):
        """
        Construct the response

        :param endpoint_context:
        :py:class:`oidcendpoint.endpoint_context.SrvInfo` instance
        :param response_args: response arguments
        :param request: The parsed request, a self.request_cls class instance
        :param kwargs: Extra keyword arguments
        :return: An instance of the self.response_cls class
        """
        response_args = self.do_pre_construct(endpoint_context, response_args,
                                              request,
                                              **kwargs)

        # logger.debug("kwargs: %s" % sanitize(kwargs))
        response = self.response_cls(**response_args)

        return self.do_post_construct(endpoint_context, response, request,
                                      **kwargs)

    def response_info(self, endpoint_context, response_args, request, **kwargs):
        return self.construct(endpoint_context, response_args, request,
                              **kwargs)

    def do_response(self, endpoint_context, response_args=None, request=None,
                    **kwargs):
        if response_args is None:
            response_args = {}

        _response = self.response_info(endpoint_context, response_args, request,
                                       **kwargs)
        if 'error' in _response:
            return _response

        if self.response_placement == 'body':
            if self.response_format == 'json':
                content_type = 'application/json'
                resp = _response.to_json()
            else:
                content_type = 'application/x-www-form-urlencoded'
                resp = _response.to_urlencoded()
        elif self.response_placement == 'url':
            # content_type = 'application/x-www-form-urlencoded'
            content_type = ''
            try:
                fragment_enc = kwargs['fragment_enc']
            except KeyError:
                fragment_enc = False

            if fragment_enc:
                resp = _response.request(kwargs['return_uri'], True)
            else:
                resp = _response.request(kwargs['return_uri'])
        else:
            raise ValueError(
                "Don't know where that is: '{}".format(self.response_placement))

        if content_type:
            try:
                http_headers = set_content_type(kwargs['http_headers'],
                                                content_type)
            except KeyError:
                http_headers = [('Content-type', content_type)]
        else:
            http_headers = []

        http_headers.extend(OAUTH2_NOCACHE_HEADERS)

        return {'response': resp, 'http_headers': http_headers}