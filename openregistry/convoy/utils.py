# -*- coding: utf-8 -*-
from logging import getLogger, addLevelName, Logger
from socket import error
from time import sleep

from couchdb import Server, Session
from munch import Munch
from pkg_resources import get_distribution

from openprocurement_client.resources.assets import AssetsClient
from openprocurement_client.resources.auctions import AuctionsClient
from openprocurement_client.resources.lots import LotsClient


addLevelName(25, 'CHECK')


def check(self, msg, exc=None, *args, **kwargs):
    self.log(25, msg)
    if exc:
        self.error(exc, exc_info=True)


Logger.check = check

PKG = get_distribution(__package__)
LOGGER = getLogger(PKG.project_name)

FILTER_DOC_ID = '_design/auction_filters'
FILTER_CONVOY_FEED_DOC = """
function(doc, req) {
    if (doc.doc_type == 'Auction') {
        if (doc.status == 'pending.verification') {
            return true;
        } else if (['complete', 'cancelled', 'unsuccessful'].indexOf(doc.status) >= 0 && doc.merchandisingObject) {
            return true;
        };
    }
    return false;
}
"""

CONTINUOUS_CHANGES_FEED_FLAG = True  # Need for testing


class ConfigError(Exception):
    pass


def prepare_couchdb(couch_url, db_name):
    server = Server(couch_url, session=Session(retry_delays=range(10)))
    try:
        if db_name not in server:
            db = server.create(db_name)
        else:
            db = server[db_name]

        push_filter_doc(db)

    except error as e:
        LOGGER.error('Database error: {}'.format(e.message))
        raise ConfigError(e.strerror)
    return db


def push_filter_doc(db):
    filters_doc = db.get(FILTER_DOC_ID, {'_id': FILTER_DOC_ID, 'filters': {}})
    if (filters_doc and filters_doc['filters'].get('convoy_feed') !=
        FILTER_CONVOY_FEED_DOC):
        filters_doc['filters']['convoy_feed'] = \
            FILTER_CONVOY_FEED_DOC
        db.save(filters_doc)
        LOGGER.info('Filter doc \'convoy_feed\' saved.')
    else:
        LOGGER.info('Filter doc \'convoy_feed\' exist.')
    LOGGER.info('Added filters doc to db.')


def continuous_changes_feed(db, timeout=10, limit=100,
                            filter_doc='auction_filters/convoy_feed'):
    last_seq_id = 0
    while CONTINUOUS_CHANGES_FEED_FLAG:
        data = db.changes(include_docs=True, since=last_seq_id, limit=limit,
                          filter=filter_doc)
        last_seq_id = data['last_seq']
        if len(data['results']) != 0:
            for row in data['results']:
                item = Munch({
                    'id': row['doc']['_id'],
                    'status': row['doc']['status'],
                    'merchandisingObject': row['doc']['merchandisingObject']
                })
                yield item
        else:
            sleep(timeout)


def init_clients(config):
    clients_from_config = {
        'auctions_client': {'section': 'auctions', 'client_instance': AuctionsClient},
        'lots_client': {'section': 'lots', 'client_instance': LotsClient},
        'assets_client': {'section': 'assets', 'client_instance': AssetsClient},
    }
    exceptions = []

    for key, item in clients_from_config.items():
        section = item['section']
        try:
            client = item['client_instance'](
                key=config[section]['api']['token'],
                host_url=config[section]['api']['url'],
                api_version=config[section]['api']['version'],
                ds_config=config[section].get('ds', None)
            )
            clients_from_config[key] = client
            result = ('ok', None)
        except Exception as e:
            exceptions.append(e)
            result = ('failed', e)
        LOGGER.check('{} - {}'.format(key, result[0]), result[1])
    if not hasattr(clients_from_config['auctions_client'], 'ds_client'):
        LOGGER.warning("Document Service configuration is not available.")

    try:
        if config['db'].get('login', '') \
                and config['db'].get('password', ''):
            db_url = "http://{login}:{password}@{host}:{port}".format(
                **config['db']
            )
            LOGGER.info('couchdb - authorized')
        else:
            db_url = "http://{host}:{port}".format(**config['db'])
            LOGGER.info('couchdb without user')

        clients_from_config['db'] = prepare_couchdb(
            db_url, config['db']['name']
        )
        result = ('ok', None)
    except Exception as e:
        exceptions.append(e)
        result = ('failed', e)
    LOGGER.check('couchdb - {}'.format(result[0]), result[1])

    if exceptions:
        raise exceptions[0]

    return clients_from_config
