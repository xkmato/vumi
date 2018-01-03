# -*- test-case-name: vumi.transports.dmark.tests.test_dmark_ussd -*-

import json

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.web import http

from vumi.components.session import SessionManager
from vumi.config import ConfigDict, ConfigInt, ConfigBool
from vumi.message import TransportUserMessage
from vumi.transports.httprpc import HttpRpcTransport


class DmarkUssdTransportConfig(HttpRpcTransport.CONFIG_CLASS):
    """Config for Dmark USSD transport."""

    ussd_session_timeout = ConfigInt(
        "Number of seconds before USSD session information stored in Redis"
        " expires.",
        default=600, static=True)

    fix_to_addr = ConfigBool(
        "Whether or not to ensure that the to_addr is always starting with a "
        "* and ending with a #", default=False, static=True)

    redis_manager = ConfigDict(
        "Redis client configuration.", default={}, static=True)


class DmarkUssdTransport(HttpRpcTransport):
    """Dmark USSD transport over HTTP.

    When a USSD message is received, Dmark will make an HTTP GET request to
    the transport with the following query parameters:

    * ``transactionId``: A unique ID for the USSD session (string).
    * ``msisdn``: The phone number that the message was sent from (string).
    * ``ussdServiceCode``: The USSD Service code the request was made to
      (string).
    * ``transactionTime``: The time the USSD request was received at Dmark,
      as a Unix timestamp (UTC).
    * ``ussdRequestString``: The full content of the USSD request(string).
    * ``creationTime``: The time the USSD request was sent, as a Unix
      timestamp (UTC), if available. (This time is given by the mobile
      network, and may not always be reliable.)
    * ``response``: ``"false"`` if this is a new session, ``"true"`` if it is
      not. Currently not used by the transport (it relies on the
      ``transactionId`` being unique instead).

    The transport may respond to this request either using JSON or form-encoded
    data. A successful response must return HTTP status code 200. Any other
    response code is treated as a failure.

    This transport responds with JSON encoded data. The JSON response
    contains the following keys:

    * ``responseString``: The content to be returned to the phone number that
      originated the USSD request.
    * ``action``: Either ``end`` or ``request``. ``end`` signifies that no
      further interaction is expected from the user and the USSD session should
      be closed. ``request`` signifies that further interaction is expected.

    **Example JSON response**:

    .. sourcecode: javascript

       {
         "responseString": "Hello from Vumi!",
         "action": "end"
       }
    """

    CONFIG_CLASS = DmarkUssdTransportConfig

    transport_type = 'ussd'

    ENCODING = 'utf-8'
    EXPECTED_FIELDS = frozenset([
        'transactionId', 'msisdn', 'ussdServiceCode', 'transactionTime',
        'ussdRequestString', 'creationTime', 'response',
    ])

    @inlineCallbacks
    def setup_transport(self):
        yield super(DmarkUssdTransport, self).setup_transport()
        config = self.get_static_config()
        r_prefix = "vumi.transports.dmark_ussd:%s" % self.transport_name
        self.session_manager = yield SessionManager.from_redis_config(
            config.redis_manager, r_prefix,
            max_session_length=config.ussd_session_timeout)

    @inlineCallbacks
    def teardown_transport(self):
        yield super(DmarkUssdTransport, self).teardown_transport()
        yield self.session_manager.stop()

    @inlineCallbacks
    def session_event_for_transaction(self, transaction_id):
        # XXX: There is currently no way to detect when the user closes
        #      the session (i.e. TransportUserMessage.SESSION_CLOSE)
        self.setup_transport()
        session_id = transaction_id
        session = yield self.session_manager.load_session(transaction_id)
        if session:
            session_event = TransportUserMessage.SESSION_RESUME
            yield self.session_manager.save_session(session_id, session)
        else:
            session_event = TransportUserMessage.SESSION_NEW
            yield self.session_manager.create_session(
                session_id, transaction_id=transaction_id)
        returnValue(session_event)

    @inlineCallbacks
    def handle_raw_inbound_message(self, request_id, request):
        try:
            values, errors = self.get_field_values(
                request, self.EXPECTED_FIELDS)
        except UnicodeDecodeError:
            self.log.msg('Bad request encoding: %r' % request)
            request_dict = {
                'uri': request.uri,
                'method': request.method,
                'path': request.path,
                'content': request.content.read(),
                'headers': dict(request.requestHeaders.getAllRawHeaders()),
            }
            self.finish_request(
                request_id, json.dumps({'invalid_request': request_dict}),
                code=http.BAD_REQUEST)
            yield self.add_status(
                component='request',
                status='down',
                type='invalid_encoding',
                message='Invalid encoding',
                details={
                    'request': request_dict,
                })
            return

        if errors:
            self.log.msg('Unhappy incoming message: %r' % (errors,))
            self.finish_request(
                request_id, json.dumps(errors), code=http.BAD_REQUEST)
            yield self.add_status(
                component='request',
                status='down',
                type='invalid_inbound_fields',
                message='Invalid inbound fields',
                details=errors)
            return

        yield self.add_status(
            component='request',
            status='ok',
            type='request_parsed',
            message='Request parsed',)

        config = self.get_static_config()
        to_addr = values["ussdServiceCode"]
        if (config.fix_to_addr
            and not to_addr.startswith('*')
                and not to_addr.endswith('#')):
            to_addr = '*%s#' % (to_addr,)
        from_addr = values["msisdn"]
        content = values["ussdRequestString"]
        session_event = yield self.session_event_for_transaction(
            values["transactionId"])
        if session_event == TransportUserMessage.SESSION_NEW:
            content = None

        yield self.publish_message(
            message_id=request_id,
            content=content,
            to_addr=to_addr,
            from_addr=from_addr,
            provider='dmark',
            session_event=session_event,
            transport_type=self.transport_type,
            helper_metadata={
                'session_id': values['transactionId'],
            },
            transport_metadata={
                'dmark_ussd': {
                    'transaction_id': values['transactionId'],
                    'transaction_time': values['transactionTime'],
                    'creation_time': values['creationTime'],
                }
            })

    @inlineCallbacks
    def handle_outbound_message(self, message):
        self.emit("DmarkUssdTransport consuming %r" % (message,))
        missing_fields = self.ensure_message_values(
            message, ['in_reply_to', 'content'])
        if missing_fields:
            nack = yield self.reject_message(message, missing_fields)
            returnValue(nack)

        if message["session_event"] == TransportUserMessage.SESSION_CLOSE:
            action = "end"
        else:
            action = "request"

        response_data = {
            "responseString": message["content"],
            "action": action,
        }

        response_id = self.finish_request(
            message['in_reply_to'], json.dumps(response_data))

        if response_id is not None:
            ack = yield self.publish_ack(
                user_message_id=message['message_id'],
                sent_message_id=message['message_id'])
            returnValue(ack)
        else:
            nack = yield self.publish_nack(
                user_message_id=message['message_id'],
                sent_message_id=message['message_id'],
                reason="Could not find original request.")
            returnValue(nack)

    def on_down_response_time(self, message_id, time):
        request = self.get_request(message_id)
        # We send different status events for error responses
        if request.code < 200 or request.code >= 300:
            return
        return self.add_status(
            component='response',
            status='down',
            type='very_slow_response',
            message='Very slow response',
            reasons=[
                'Response took longer than %fs' % (
                    self.response_time_down,)
            ],
            details={
                'response_time': time,
            })

    def on_degraded_response_time(self, message_id, time):
        request = self.get_request(message_id)
        # We send different status events for error responses
        if request.code < 200 or request.code >= 300:
            return
        return self.add_status(
            component='response',
            status='degraded',
            type='slow_response',
            message='Slow response',
            reasons=[
                'Response took longer than %fs' % (
                    self.response_time_degraded,)
            ],
            details={
                'response_time': time,
            })

    def on_good_response_time(self, message_id, time):
        request = self.get_request(message_id)
        # We send different status events for error responses
        if request.code < 200 or request.code >= 400:
            return
        return self.add_status(
            component='response',
            status='ok',
            type='response_sent',
            message='Response sent',
            details={
                'response_time': time,
            })

    def on_timeout(self, message_id, time):
        return self.add_status(
            component='response',
            status='down',
            type='timeout',
            message='Response timed out',
            reasons=[
                'Response took longer than %fs' % (
                    self.request_timeout,)
            ],
            details={
                'response_time': time,
            })
