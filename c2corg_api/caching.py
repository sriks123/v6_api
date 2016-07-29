import logging
from dogpile.cache import make_region

log = logging.getLogger(__name__)

# prefix for all cache keys
KEY_PREFIX = 'c2corg'


def create_region(name):
    return make_region(
        # prefix all keys (e.g. returns 'c2corg_main:detail:38575-1')
        key_mangler=lambda key: '{0}:{1}:{2}'.format(KEY_PREFIX, name, key)
    )

cache_document_detail = create_region('detail')
cache_document_listing = create_region('listing')
cache_document_history = create_region('history')

caches = [
    cache_document_detail,
    cache_document_listing,
    cache_document_history
]


def configure_caches(settings):
    global KEY_PREFIX
    KEY_PREFIX = settings['redis.cache_key_prefix']

    log.debug('Redis: {0}'.format(settings['redis.url']))

    for cache in caches:
        # TODO use connection pool
        cache.configure(
            'dogpile.cache.redis',
            arguments={
                'url': settings['redis.url'],
                'distributed_lock': True,
                'socket_timeout': 3,  # 3 seconds
                'lock_timeout': 5  # 5 seconds
            },
            replace_existing_backend=True
        )
        cache.invalidate()
