#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import gc
import logging
import os
import sys

import synapse
import synapse.config.logger
from synapse import events
from synapse.api.urls import CONTENT_REPO_PREFIX, FEDERATION_PREFIX, \
    LEGACY_MEDIA_PREFIX, MEDIA_PREFIX, SERVER_KEY_PREFIX, SERVER_KEY_V2_PREFIX, \
    STATIC_PREFIX, WEB_CLIENT_PREFIX
from synapse.app import _base
from synapse.app._base import quit_with_error
from synapse.config._base import ConfigError
from synapse.config.homeserver import HomeServerConfig
from synapse.crypto import context_factory
from synapse.federation.transport.server import TransportLayerServer
from synapse.module_api import ModuleApi
from synapse.http.additional_resource import AdditionalResource
from synapse.http.server import RootRedirect
from synapse.http.site import SynapseSite
from synapse.metrics import register_memory_metrics
from synapse.metrics.resource import METRICS_PREFIX, MetricsResource
from synapse.python_dependencies import CONDITIONAL_REQUIREMENTS, \
    check_requirements
from synapse.replication.tcp.resource import ReplicationStreamProtocolFactory
from synapse.rest import ClientRestResource
from synapse.rest.key.v1.server_key_resource import LocalKey
from synapse.rest.key.v2 import KeyApiV2Resource
from synapse.rest.media.v0.content_repository import ContentRepoResource
from synapse.rest.media.v1.media_repository import MediaRepositoryResource
from synapse.server import HomeServer
from synapse.storage import are_all_users_on_domain
from synapse.storage.engines import IncorrectDatabaseSetup, create_engine
from synapse.storage.prepare_database import UpgradeDatabaseException, prepare_database
from synapse.util.httpresourcetree import create_resource_tree
from synapse.util.logcontext import LoggingContext
from synapse.util.manhole import manhole
from synapse.util.module_loader import load_module
from synapse.util.rlimit import change_resource_limit
from synapse.util.versionstring import get_version_string
from twisted.application import service
from twisted.internet import defer, reactor
from twisted.web.resource import EncodingResourceWrapper, Resource
from twisted.web.server import GzipEncoderFactory
from twisted.web.static import File

from autobahn.twisted.resource import WebSocketResource
from synapse.websocket.websocket import SynapseWebsocketFactory

logger = logging.getLogger("synapse.app.homeserver")


def gz_wrap(r):
    return EncodingResourceWrapper(r, [GzipEncoderFactory()])


def build_resource_for_web_client(hs):
    webclient_path = hs.get_config().web_client_location
    if not webclient_path:
        try:
            import syweb
        except ImportError:
            quit_with_error(
                "Could not find a webclient.\n\n"
                "Please either install the matrix-angular-sdk or configure\n"
                "the location of the source to serve via the configuration\n"
                "option `web_client_location`\n\n"
                "To install the `matrix-angular-sdk` via pip, run:\n\n"
                "    pip install '%(dep)s'\n"
                "\n"
                "You can also disable hosting of the webclient via the\n"
                "configuration option `web_client`\n"
                % {"dep": CONDITIONAL_REQUIREMENTS["web_client"].keys()[0]}
            )
        syweb_path = os.path.dirname(syweb.__file__)
        webclient_path = os.path.join(syweb_path, "webclient")
    # GZip is disabled here due to
    # https://twistedmatrix.com/trac/ticket/7678
    # (It can stay enabled for the API resources: they call
    # write() with the whole body and then finish() straight
    # after and so do not trigger the bug.
    # GzipFile was removed in commit 184ba09
    # return GzipFile(webclient_path)  # TODO configurable?
    return File(webclient_path)  # TODO configurable?


class SynapseHomeServer(HomeServer):
    def _listener_http(self, config, listener_config):
        port = listener_config["port"]
        bind_addresses = listener_config["bind_addresses"]
        tls = listener_config.get("tls", False)
        site_tag = listener_config.get("tag", port)

        if tls and config.no_tls:
            return

        resources = {}
        for res in listener_config["resources"]:
            for name in res["names"]:
                resources.update(self._configure_named_resource(
                    name, res.get("compress", False),
                    listener_config.get("x_forwarded", False),
                ))

        additional_resources = listener_config.get("additional_resources", {})
        logger.debug("Configuring additional resources: %r",
                     additional_resources)
        module_api = ModuleApi(self, self.get_auth_handler())
        for path, resmodule in additional_resources.items():
            handler_cls, config = load_module(resmodule)
            handler = handler_cls(config, module_api)
            resources[path] = AdditionalResource(self, handler.handle_request)

        if WEB_CLIENT_PREFIX in resources:
            root_resource = RootRedirect(WEB_CLIENT_PREFIX)
        else:
            root_resource = Resource()

        root_resource = create_resource_tree(resources, root_resource)

        if tls:
            for address in bind_addresses:
                reactor.listenSSL(
                    port,
                    SynapseSite(
                        "synapse.access.https.%s" % (site_tag,),
                        site_tag,
                        listener_config,
                        root_resource,
                    ),
                    self.tls_server_context_factory,
                    interface=address
                )
        else:
            for address in bind_addresses:
                reactor.listenTCP(
                    port,
                    SynapseSite(
                        "synapse.access.http.%s" % (site_tag,),
                        site_tag,
                        listener_config,
                        root_resource,
                    ),
                    interface=address
                )
        logger.info("Synapse now listening on port %d", port)

    def _configure_named_resource(self, name, compress=False, proxied=False):
        """Build a resource map for a named resource

        Args:
            name (str): named resource: one of "client", "federation", etc
            compress (bool): whether to enable gzip compression for this
                resource

        Returns:
            dict[str, Resource]: map from path to HTTP resource
        """
        resources = {}
        if name == "client":
            client_resource = ClientRestResource(self)
            if compress:
                client_resource = gz_wrap(client_resource)

            resources.update({
                "/_matrix/client/api/v1": client_resource,
                "/_matrix/client/r0": client_resource,
                "/_matrix/client/unstable": client_resource,
                "/_matrix/client/v2_alpha": client_resource,
                "/_matrix/client/versions": client_resource,
            })

        if name == "websocket":
            ws_factory = SynapseWebsocketFactory(self, compress, proxied)
            ws_factory.startFactory()
            websocket_resource = WebSocketResource(ws_factory)
            resources.update({
                "/_matrix/client/ws/r0": websocket_resource,
            })

        if name == "federation":
            resources.update({
                FEDERATION_PREFIX: TransportLayerServer(self),
            })

        if name in ["static", "client"]:
            resources.update({
                STATIC_PREFIX: File(
                    os.path.join(os.path.dirname(synapse.__file__), "static")
                ),
            })

        if name in ["media", "federation", "client"]:
            media_repo = MediaRepositoryResource(self)
            resources.update({
                MEDIA_PREFIX: media_repo,
                LEGACY_MEDIA_PREFIX: media_repo,
                CONTENT_REPO_PREFIX: ContentRepoResource(
                    self, self.config.uploads_path
                ),
            })

        if name in ["keys", "federation"]:
            resources.update({
                SERVER_KEY_PREFIX: LocalKey(self),
                SERVER_KEY_V2_PREFIX: KeyApiV2Resource(self),
            })

        if name == "webclient":
            resources[WEB_CLIENT_PREFIX] = build_resource_for_web_client(self)

        if name == "metrics" and self.get_config().enable_metrics:
            resources[METRICS_PREFIX] = MetricsResource(self)

        return resources

    def start_listening(self):
        config = self.get_config()

        for listener in config.listeners:
            if listener["type"] == "http":
                self._listener_http(config, listener)
            elif listener["type"] == "manhole":
                bind_addresses = listener["bind_addresses"]

                for address in bind_addresses:
                    reactor.listenTCP(
                        listener["port"],
                        manhole(
                            username="matrix",
                            password="rabbithole",
                            globals={"hs": self},
                        ),
                        interface=address
                    )
            elif listener["type"] == "replication":
                bind_addresses = listener["bind_addresses"]
                for address in bind_addresses:
                    factory = ReplicationStreamProtocolFactory(self)
                    server_listener = reactor.listenTCP(
                        listener["port"], factory, interface=address
                    )
                    reactor.addSystemEventTrigger(
                        "before", "shutdown", server_listener.stopListening,
                    )
            else:
                logger.warn("Unrecognized listener type: %s", listener["type"])

    def run_startup_checks(self, db_conn, database_engine):
        all_users_native = are_all_users_on_domain(
            db_conn.cursor(), database_engine, self.hostname
        )
        if not all_users_native:
            quit_with_error(
                "Found users in database not native to %s!\n"
                "You cannot changed a synapse server_name after it's been configured"
                % (self.hostname,)
            )

        try:
            database_engine.check_database(db_conn.cursor())
        except IncorrectDatabaseSetup as e:
            quit_with_error(e.message)

    def get_db_conn(self, run_new_connection=True):
        # Any param beginning with cp_ is a parameter for adbapi, and should
        # not be passed to the database engine.
        db_params = {
            k: v for k, v in self.db_config.get("args", {}).items()
            if not k.startswith("cp_")
        }
        db_conn = self.database_engine.module.connect(**db_params)

        if run_new_connection:
            self.database_engine.on_new_connection(db_conn)
        return db_conn


def setup(config_options):
    """
    Args:
        config_options_options: The options passed to Synapse. Usually
            `sys.argv[1:]`.

    Returns:
        HomeServer
    """
    try:
        config = HomeServerConfig.load_or_generate_config(
            "Synapse Homeserver",
            config_options,
        )
    except ConfigError as e:
        sys.stderr.write("\n" + e.message + "\n")
        sys.exit(1)

    if not config:
        # If a config isn't returned, and an exception isn't raised, we're just
        # generating config files and shouldn't try to continue.
        sys.exit(0)

    synapse.config.logger.setup_logging(config, use_worker_options=False)

    # check any extra requirements we have now we have a config
    check_requirements(config)

    version_string = "Synapse/" + get_version_string(synapse)

    logger.info("Server hostname: %s", config.server_name)
    logger.info("Server version: %s", version_string)

    events.USE_FROZEN_DICTS = config.use_frozen_dicts

    tls_server_context_factory = context_factory.ServerContextFactory(config)

    database_engine = create_engine(config.database_config)
    config.database_config["args"]["cp_openfun"] = database_engine.on_new_connection

    hs = SynapseHomeServer(
        config.server_name,
        db_config=config.database_config,
        tls_server_context_factory=tls_server_context_factory,
        config=config,
        version_string=version_string,
        database_engine=database_engine,
    )

    logger.info("Preparing database: %s...", config.database_config['name'])

    try:
        db_conn = hs.get_db_conn(run_new_connection=False)
        prepare_database(db_conn, database_engine, config=config)
        database_engine.on_new_connection(db_conn)

        hs.run_startup_checks(db_conn, database_engine)

        db_conn.commit()
    except UpgradeDatabaseException:
        sys.stderr.write(
            "\nFailed to upgrade database.\n"
            "Have you checked for version specific instructions in"
            " UPGRADES.rst?\n"
        )
        sys.exit(1)

    logger.info("Database prepared in %s.", config.database_config['name'])

    hs.setup()
    hs.start_listening()

    def start():
        hs.get_pusherpool().start()
        hs.get_state_handler().start_caching()
        hs.get_datastore().start_profiling()
        hs.get_datastore().start_doing_background_updates()
        hs.get_replication_layer().start_get_pdu_cache()

        register_memory_metrics(hs)

    reactor.callWhenRunning(start)

    return hs


class SynapseService(service.Service):
    """A twisted Service class that will start synapse. Used to run synapse
    via twistd and a .tac.
    """
    def __init__(self, config):
        self.config = config

    def startService(self):
        hs = setup(self.config)
        change_resource_limit(hs.config.soft_file_limit)
        if hs.config.gc_thresholds:
            gc.set_threshold(*hs.config.gc_thresholds)

    def stopService(self):
        return self._port.stopListening()


def run(hs):
    PROFILE_SYNAPSE = False
    if PROFILE_SYNAPSE:
        def profile(func):
            from cProfile import Profile
            from threading import current_thread

            def profiled(*args, **kargs):
                profile = Profile()
                profile.enable()
                func(*args, **kargs)
                profile.disable()
                ident = current_thread().ident
                profile.dump_stats("/tmp/%s.%s.%i.pstat" % (
                    hs.hostname, func.__name__, ident
                ))

            return profiled

        from twisted.python.threadpool import ThreadPool
        ThreadPool._worker = profile(ThreadPool._worker)
        reactor.run = profile(reactor.run)

    clock = hs.get_clock()
    start_time = clock.time()

    stats = {}

    @defer.inlineCallbacks
    def phone_stats_home():
        logger.info("Gathering stats for reporting")
        now = int(hs.get_clock().time())
        uptime = int(now - start_time)
        if uptime < 0:
            uptime = 0

        stats["homeserver"] = hs.config.server_name
        stats["timestamp"] = now
        stats["uptime_seconds"] = uptime
        stats["total_users"] = yield hs.get_datastore().count_all_users()

        total_nonbridged_users = yield hs.get_datastore().count_nonbridged_users()
        stats["total_nonbridged_users"] = total_nonbridged_users

        room_count = yield hs.get_datastore().get_room_count()
        stats["total_room_count"] = room_count

        stats["daily_active_users"] = yield hs.get_datastore().count_daily_users()
        stats["daily_active_rooms"] = yield hs.get_datastore().count_daily_active_rooms()
        stats["daily_messages"] = yield hs.get_datastore().count_daily_messages()

        daily_sent_messages = yield hs.get_datastore().count_daily_sent_messages()
        stats["daily_sent_messages"] = daily_sent_messages

        logger.info("Reporting stats to matrix.org: %s" % (stats,))
        try:
            yield hs.get_simple_http_client().put_json(
                "https://matrix.org/report-usage-stats/push",
                stats
            )
        except Exception as e:
            logger.warn("Error reporting stats: %s", e)

    if hs.config.report_stats:
        logger.info("Scheduling stats reporting for 3 hour intervals")
        clock.looping_call(phone_stats_home, 3 * 60 * 60 * 1000)

        # We wait 5 minutes to send the first set of stats as the server can
        # be quite busy the first few minutes
        clock.call_later(5 * 60, phone_stats_home)

    if hs.config.daemonize and hs.config.print_pidfile:
        print (hs.config.pid_file)

    _base.start_reactor(
        "synapse-homeserver",
        hs.config.soft_file_limit,
        hs.config.gc_thresholds,
        hs.config.pid_file,
        hs.config.daemonize,
        hs.config.cpu_affinity,
        logger,
    )


def main():
    with LoggingContext("main"):
        # check base requirements
        check_requirements()
        hs = setup(sys.argv[1:])
        run(hs)


if __name__ == '__main__':
    main()
