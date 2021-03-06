import json
import logging

from autobahn.twisted.websocket import WebSocketServerFactory, WebSocketServerProtocol
from autobahn.websocket.compress import (
    PerMessageDeflateOffer,
    PerMessageDeflateOfferAccept,
)

from twisted.internet import defer

from synapse.api.constants import EventTypes, PresenceState
from synapse.api.errors import AuthError, Codes, SynapseError
from synapse.api.filtering import DEFAULT_FILTER_COLLECTION, FilterCollection
from synapse.handlers.sync import SyncConfig
from synapse.metrics import LaterGauge
from synapse.rest.client.transactions import HttpTransactionCache
from synapse.rest.client.v2_alpha._base import set_timeline_upper_limit
from synapse.rest.client.v2_alpha.sync import SyncRestServlet
from synapse.types import StreamToken, UserID, create_requester

logger = logging.getLogger("synapse.websocket")

# Close Reason Codes:
# 3001 - No Access Token
# 3002 - Unknown Access Token
# 3003 - Generic failure trying to auth.

ERR_NO_AT = unicode("No access_token provided.")
ERR_UNKNOWN_AT = unicode("Unknown access_token.")
ERR_UNKNOWN_FAIL = unicode("Unknown failure trying to auth.")
SYNC_TIMEOUT = 90000


class SynapseWebsocketProtocol(WebSocketServerProtocol):
    def __init__(self):
        super(SynapseWebsocketProtocol, self).__init__()
        """
        This is run per each connection.
        """
        self.since = None
        self.currentSync = None
        self.filter = DEFAULT_FILTER_COLLECTION

    @defer.inlineCallbacks
    def onConnect(self, request):
        # these are here as factory is not available in __init__
        self.hs = self.factory.hs
        self.auth = self.hs.get_auth()
        self.clock = self.hs.get_clock()
        self.datastore = self.hs.get_datastore()
        self.filtering = self.hs.get_filtering()
        self.presence_handler = self.hs.get_presence_handler()
        self.reactor = self.hs.get_reactor()
        self.sync_handler = self.hs.get_sync_handler()

        logger.info("connecting: {0}".format(request.peer))

        if self.factory.proxied:
            ip_addr = request.headers.get('x-forwarded-for', request.host)
        else:
            ip_addr = request.host

        user_agent = request.headers.get("user-agent", [""])[0]

        logger.info("Checking access_token for {0}".format(ip_addr))
        access_token = request.params.get("access_token", [None])[0]
        if access_token is None:
            self.sendClose(3001, ERR_NO_AT)
            return
        access_token = access_token.decode('utf-8')

        user = None
        try:
            user = yield self.auth.get_user_by_access_token(access_token)
            self.requester = create_requester(
                user["user"],
                user["token_id"],
                user["is_guest"],
                user.get("device_id"),
                app_service=None
            )
        except AuthError as ex:
            self.sendClose(3002, ERR_UNKNOWN_AT)
            logger.info("Closing due to auth error %s" % ex)
            return
        except Exception as ex:
            self.sendClose(3003, ERR_UNKNOWN_FAIL)
            logger.info("Closing due to unknown error %s" % ex)
            return
        logger.info("authenticated {0} ({1}) okay".format(user['user'], ip_addr))

        self.full_state = request.params.get("full_state", [False])[0]

        since = request.params.get("since", [None])[0]
        if since is not None:
            since = since.decode('utf-8')
            self.since = StreamToken.from_string(since)

        filter_id = request.params.get("filter", [None])[0]
        if filter_id:
            if filter_id.startswith('{'):
                try:
                    filter_object = json.loads(filter_id)
                    set_timeline_upper_limit(
                        filter_object,
                        self.factory.hs.config.filter_timeline_limit
                    )
                except Exception:
                    raise SynapseError(400, "Invalid filter JSON")
                self.factory.filtering.check_valid_filter(filter_object)
                self.filter = FilterCollection(filter_object)
            else:
                self.filter = yield self.factory.filtering.get_user_filter(
                    user['user'].localpart, filter_id
                )
            self.filter_id = filter_id

        if user and access_token and ip_addr:
            self.datastore.insert_client_ip(
                user_id=user["user"].to_string(),
                access_token=access_token,
                ip=ip_addr,
                user_agent=user_agent,
                device_id=user.get("device_id"),
            )

        presence = request.params.get("presence", [PresenceState.ONLINE])[0]
        logger.debug("Presence should be: %s" % presence)
        if presence != PresenceState.OFFLINE:
            yield self.presence_handler.set_state(
                user['user'], {"presence": presence}, True
            )
        self.presence = presence

        if request.protocols:
            if "m.json" in request.protocols:
                defer.returnValue(("m.json"))
            else:
                msg = "None of the passed websocket protocols is allowed ({0})".format(
                    json.dumps(request.protocols)
                )
                raise Exception(msg)
        else:
            # No protocol was passed so just allow handling
            defer.returnValue(None)

    def onOpen(self):
        logger.info("New connection.")
        self.shouldSync = False
        self.startSyncingClient()

    @defer.inlineCallbacks
    def onMessage(self, payload, isBinary):
        if isBinary:
            logger.debug("Binary message received: {0} bytes".format(len(payload)))
            return  # Ignore binary for now
        else:
            try:
                logger.debug("Text message received: {0}".format(payload.decode('utf8')))
            except Exception:
                logger.debug("Text message received (unparseable)")

        msg = {}
        try:
            msg = json.loads(payload.decode('utf8'))
        except Exception as ex:
            logger.warn("Received payload is not json")
            return

        supported_methods = {
            "ping": self._handle_ping,
            "presence": self._handle_presence,
            "read_markers": self._handle_read_markers,
            "send": self._handle_send,
            "state": self._handle_state,
            "typing": self._handle_typing,
        }

        method = supported_methods.get(
            msg["method"],
            lambda msg: json.dumps({
                "id": msg["id"],
                "error": {
                    "errcode": "M_BAD_JSON",
                    "error": "Unknown method",
                }
            })
        )

        try:
            result = yield method(msg)
            self.sendMessage(result)
        except SynapseError as ex:
            self.sendMessage(json.dumps({
                "id": msg["id"],
                "error": {
                    "errcode": ex.errcode,
                    "error": ex.msg,
                }
            }))
        except Exception as ex:
            self.sendMessage(json.dumps({
                "id": msg["id"],
                "error": {
                    "errcode": "M_UNKNOWN",
                    "error": ex.__str__(),
                }
            }))

    def _genTxnKey(self, msg_id):
        return self.requester.user.to_string() + "/" + \
            str(self.requester.access_token_id) + "/" + msg_id

    def onClose(self, wasClean, code, reason):
        logger.info("WebSocket connection closed: {0} {1}".format(code, reason))
        self.shouldSync = False
        if self.currentSync is not None:
            self.currentSync.cancel()

    def startSyncingClient(self):
        logger.info("Started syncing for %s." % self.peer)
        self.shouldSync = True
        self._sync(initial=True)

    def _sync(self, initial=False):
        request_key = (
            self.requester.user,
            0,  # timeout
            self.since,
            self.filter_id,
            self.full_state if initial else False,
            self.requester.device_id,
        )
        sync_config = SyncConfig(
            user=self.requester.user,
            filter_collection=self.filter,
            is_guest=self.requester.is_guest,
            request_key=request_key,
            device_id=self.requester.device_id,
        )

        logger.debug("Syncing with %s" % str(self.since))

        affect_presence = self.presence != PresenceState.OFFLINE

        @defer.inlineCallbacks
        def sync_with_presence_context():
            context = yield self.presence_handler.user_syncing(
                self.requester.user.to_string(), affect_presence=affect_presence,
            )
            with context:
                try:
                    sync_result = yield self.sync_handler.wait_for_sync_for_user(
                        sync_config,
                        since_token=self.since,
                        timeout=0 if initial else SYNC_TIMEOUT,
                        full_state=self.full_state if initial else False
                    )
                except Exception as ex:
                    logger.warn("Something went wrong when getting sync response: %s", ex)

                defer.returnValue(sync_result)

        sync = defer.maybeDeferred(sync_with_presence_context)
        sync.addCallback(self._sync_callback)
        logger.debug("Returning from _sync")
        self.currentSync = sync

    def _sync_callback(self, result):
        logger.debug("Got sync")
        if self.shouldSync:
            self.since = result.next_batch
            time_now = self.clock.time_msec()

            logger.debug("Sending sync")
            self.sendMessage(json.dumps(SyncRestServlet.encode_response(
                time_now,
                result,
                self.requester.access_token_id,
                self.filter
            )), False)

            # start new call of _sync - use reactor to avoid endless recursion
            self.reactor.callLater(0, self._sync)

            logger.debug("Returning from _sync_callback")

    @defer.inlineCallbacks
    def _handle_ping(self, msg):
        yield logger.debug("Execute _handle_ping")
        defer.returnValue(bytes('{"id":"' + msg["id"] + '","result":{}}'))

    @defer.inlineCallbacks
    def _handle_presence(self, msg):
        yield logger.debug("Execute _handle_presence")

        state = {}
        params = msg["params"]

        try:
            state["presence"] = params.pop("presence")

            if "status_msg" in params:
                state["status_msg"] = params.pop("status_msg")
                if not isinstance(state["status_msg"], basestring):
                    raise SynapseError(400, "status_msg must be a string.")
            if params:
                raise SynapseError(400, "Too many keys", errcode=Codes.BAD_JSON)
        except SynapseError as e:
            raise e
        except Exception:
            raise SynapseError(400, "Unable to parse state")

        yield self.presence_handler.set_state(self.requester.user, state)
        self.presence = state["presence"]
        defer.returnValue(bytes('{"id":"' + msg["id"] + '","result":{}}'))

    @defer.inlineCallbacks
    def _handle_read_markers(self, msg):
        yield logger.debug("Execute _handle_read_markers")

        yield self.presence_handler.bump_presence_active_time(self.requester.user)

        params = msg["params"]
        read_event_id = params.get("m.read", None)
        if read_event_id:
            yield self.hs.get_receipts_handler().received_client_receipt(
                params["room_id"],
                "m.read",
                user_id=self.requester.user.to_string(),
                event_id=read_event_id
            )

        read_marker_event_id = params.get("m.fully_read", None)
        if read_marker_event_id:
            yield self.hs.get_read_marker_handler().received_client_read_marker(
                params["room_id"],
                user_id=self.requester.user.to_string(),
                event_id=read_marker_event_id
            )

        defer.returnValue(bytes('{"id":"' + msg["id"] + '","result":{}}'))

    @defer.inlineCallbacks
    def _handle_send(self, msg, use_cached=True):
        logger.debug("Execute _handle_send")
        if use_cached:
            result = yield self.factory.txns.fetch_or_execute(
                self._genTxnKey(msg["id"]),
                self._handle_send,
                msg,
                use_cached=False,
            )
            defer.returnValue(result)

        params = msg["params"]

        yield self.presence_handler.bump_presence_active_time(self.requester.user)

        event = yield self.hs.get_event_creation_handler()\
            .create_and_send_nonmember_event(
            self.requester,
            {
                "type": params["event_type"],
                "content": params["content"],
                "room_id": params["room_id"],
                "sender": self.requester.user.to_string(),
            },
            txn_id=msg["id"],
        )

        defer.returnValue(json.dumps({
            "id": msg["id"],
            "result": {
                "event_id": event.event_id
            }
        }))

    @defer.inlineCallbacks
    def _handle_state(self, msg, use_cached=True):
        logger.debug("Execute _handle_state")
        if use_cached:
            result = yield self.factory.txns.fetch_or_execute(
                self._genTxnKey(msg["id"]),
                self._handle_state,
                msg,
                use_cached=False,
            )
            defer.returnValue(result)

        yield self.presence_handler.bump_presence_active_time(self.requester.user)

        params = msg["params"]
        event_dict = {
            "type": params["event_type"],
            "content": params["content"],
            "room_id": params["room_id"],
            "sender": self.requester.user.to_string(),
        }
        if params["state_key"] is not None:
            event_dict["state_key"] = params["state_key"]

        if params["event_type"] == EventTypes.Member:
            membership = params["content"].get("membership", None)
            event = yield self.hs.get_room_member_handler().update_membership(
                self.requester,
                target=UserID.from_string(params["state_key"]),
                room_id=params["room_id"],
                action=membership,
                content=params["content"],
            )
        else:
            event = yield self.hs.get_event_creation_handler()\
                .create_and_send_nonmember_event(
                self.requester,
                event_dict,
                txn_id=msg["id"],
            )

        ret = {"id": msg["id"]}
        if event:
            ret["result"] = {"event_id": event.event_id}
        else:
            ret["error"] = {}

        defer.returnValue(json.dumps(ret))

    @defer.inlineCallbacks
    def _handle_typing(self, msg):
        logger.debug("Execute _handle_typing")
        params = msg["params"]

        # Limit timeout to stop people from setting silly typing timeouts.
        timeout = min(params.get("timeout", 30000), 120000)

        yield self.presence_handler.bump_presence_active_time(self.requester.user)

        if params["typing"]:
            yield self.hs.get_typing_handler().started_typing(
                target_user=self.requester.user,
                auth_user=self.requester.user,
                room_id=params["room_id"],
                timeout=timeout,
            )
        else:
            yield self.hs.get_typing_handler().stopped_typing(
                target_user=self.requester.user,
                auth_user=self.requester.user,
                room_id=params["room_id"],
            )

        defer.returnValue(bytes('{"id":"' + msg["id"] + '","result":{}}'))


class SynapseWebsocketFactory(WebSocketServerFactory):
    def __init__(self, hs, compress=False, proxied=False):
        super(SynapseWebsocketFactory, self).__init__()
        self.protocol = SynapseWebsocketProtocol
        self.hs = hs
        self.proxied = proxied

        if compress:
            self.setProtocolOptions(perMessageCompressionAccept=self.accept_compress)

        # this should be shared between all connections
        self.txns = HttpTransactionCache(hs)

        LaterGauge(
            "synapse_websocket_connection_count",
            "", [], self.getConnectionCount
        )

    @staticmethod
    def accept_compress(offers):
        for offer in offers:
            if isinstance(offer, PerMessageDeflateOffer):
                return PerMessageDeflateOfferAccept(offer)
