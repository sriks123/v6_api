import sys
import os
import logging
import transaction

from pyramid.paster import get_appsettings
from sqlalchemy import engine_from_config
from sqlalchemy.sql.expression import and_, or_, any_
from sqlalchemy.sql.functions import func

from c2corg_api.models import DBSession
from c2corg_api.models.area_association import AreaAssociation
from c2corg_api.models.association import AssociationLog
from c2corg_api.models.cache_version import \
    update_cache_version_direct, update_cache_version_full
from c2corg_api.models.document_history import HistoryMetaData
from c2corg_api.models.feed import DocumentChange, FollowedUser, FilterArea
from c2corg_api.models.mailinglist import Mailinglist
from c2corg_api.models.token import Token
from c2corg_api.models.user import User
from c2corg_api.models.user_profile import UserProfile, \
    ArchiveUserProfile, USERPROFILE_TYPE
from c2corg_api.search import get_queue_config
from c2corg_api.search.notify_sync import notify_es_syncer
from c2corg_api.views.document_delete import remove_whole_document, \
    remove_from_cache, update_deleted_documents_list
from c2corg_api.views.document_merge import transfer_associations


def usage(argv):
    cmd = os.path.basename(argv[0])
    exit('usage: {} <source_user_id> <target_user_id>\n'
         '(example: {} 123456 811780")'.format(cmd, cmd))


def exit(msg):
    print(msg)
    sys.exit(1)


def main(argv=sys.argv):
    if len(argv) < 3:
        usage(argv)

    source_user_id = int(argv[1])
    target_user_id = int(argv[2])

    if source_user_id == target_user_id:
        exit('ERROR: source and target user accounts cannot be the same')

    settings_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '../../../production.ini')
    settings = get_appsettings(settings_file)

    engine = engine_from_config(settings, 'sqlalchemy.')
    DBSession.configure(bind=engine)

    queue_config = get_queue_config(settings)

    logging.basicConfig()
    logging.getLogger('sqlalchemy.engine').setLevel(logging.WARN)

    source_user = DBSession.query(User).get(source_user_id)
    if not source_user:
        exit('ERROR: source user account (id {}) does not exist'.format(
            source_user_id))

    target_user = DBSession.query(User).get(target_user_id)
    if not target_user:
        exit('ERROR: target user account (id {}) does not exist'.format(
            target_user_id))

    sys.stdout.write(
        '\n'
        'Are you sure you want to merge the following user accounts? [y/N]\n'
        'source: id {}: {}/{}\n'
        'target: id {}: {}/{}\n'.format(
            source_user.id, source_user.name, source_user.forum_username,
            target_user.id, target_user.name, target_user.forum_username))
    if input().lower()[:1] != 'y':
        exit('ABORTED: User accounts merging has been aborted')

    print('Merging user account {} to user account {} in progress.\n'
          'Please wait...'.format(source_user_id, target_user_id))

    with transaction.manager:
        merge_user_accounts(source_user_id, target_user_id, queue_config)

    print('SUCCESS: User account {} has been merged to user account {}'.format(
        source_user_id, target_user_id))


def merge_user_accounts(source_user_id, target_user_id, queue_config):
    print('Removing from cache...')
    remove_from_cache(source_user_id)
    print('Removing geo associations...')
    _remove_geo_associations(source_user_id)
    print('Transfering associations...')
    _transfer_associations(source_user_id, target_user_id)
    print('Updating feed entries...')
    _update_feed_entries(source_user_id, target_user_id)
    print('Updating contributions versions and histories...')
    _update_history_metadata(source_user_id, target_user_id)
    print('Unregistering from mailing lists...')
    _unregister_from_mailinglists(source_user_id)
    print('Removing profile and user account...')
    _remove_user_account(source_user_id)

    # update the cache version for the source and target user accounts
    print('Updating associated documents cache...')
    update_cache_version_direct(source_user_id)
    update_cache_version_full(target_user_id, USERPROFILE_TYPE)

    # notify ES the source account no longer exists
    print('Notifying Elastic Search...')
    notify_es_syncer(queue_config)

    # TODO reassign posts/topics + remove source account in Discourse


def _remove_geo_associations(user_id):
    DBSession.query(AreaAssociation). \
        filter(AreaAssociation.document_id == user_id).delete()


def _transfer_associations(source_user_id, target_user_id):
    transfer_associations(source_user_id, target_user_id)
    # Also reassign the association author in association logs
    # to the target user:
    DBSession.query(AssociationLog). \
        filter(AssociationLog.user_id == source_user_id). \
        update({AssociationLog.user_id: target_user_id})


def _update_feed_entries(source_user_id, target_user_id):
    # Transfer feed entries to the target user only if no similar entry
    # already exists in the target user feed items.
    shared_doc_ids = DBSession.query(DocumentChange.document_id). \
        filter(DocumentChange.user_id == target_user_id). \
        intersect(
            DBSession.query(DocumentChange.document_id).
            filter(DocumentChange.user_id == source_user_id)
        ).subquery()
    DBSession.execute(
        DocumentChange.__table__.update().where(and_(
            DocumentChange.user_id == source_user_id,
            ~DocumentChange.document_id.in_(shared_doc_ids)
        )).values({
            DocumentChange.user_id: target_user_id
        })
    )
    # Remove remaining feed items since they already exist for the target user
    DBSession.query(DocumentChange). \
        filter(DocumentChange.user_id == source_user_id).delete()

    # If target user_id is already in the list of users associated
    # to feed items (user_ids), simply remove source user_id from the list:
    DBSession.execute(
        DocumentChange.__table__.update().where(and_(
            any_(DocumentChange.user_ids) == source_user_id,
            any_(DocumentChange.user_ids) == target_user_id
        )).values({
            DocumentChange.user_ids: func.array_remove(
                DocumentChange.user_ids, source_user_id)
        })
    )
    # Else replace source user_id by target user_id in user_ids
    DBSession.execute(
        DocumentChange.__table__.update().where(
            any_(DocumentChange.user_ids) == source_user_id
        ).values({
            DocumentChange.user_ids: func.array_replace(
                DocumentChange.user_ids, source_user_id, target_user_id)
        })
    )

    # Remove subscriptions to/of the source user
    DBSession.query(FollowedUser). \
        filter(or_(
            FollowedUser.followed_user_id == source_user_id,
            FollowedUser.follower_user_id == source_user_id
        )).delete()

    # Remove feed filter prefs
    DBSession.query(FilterArea). \
        filter(FilterArea.user_id == source_user_id).delete()


def _update_history_metadata(source_user_id, target_user_id):
    DBSession.query(HistoryMetaData). \
        filter(HistoryMetaData.user_id == source_user_id). \
        update({HistoryMetaData.user_id: target_user_id})


def _unregister_from_mailinglists(user_id):
    email = DBSession.query(User.email). \
        filter(User.id == user_id).subquery()
    DBSession.execute(
        Mailinglist.__table__.delete().where(Mailinglist.email == email)
    )


def _remove_user_account(user_id):
    DBSession.query(Token).filter(Token.userid == user_id).delete()
    DBSession.query(User).filter(User.id == user_id).delete()
    # Delete profile document and its archives
    remove_whole_document(user_id, UserProfile, None,
                          ArchiveUserProfile, None)
    update_deleted_documents_list(user_id, USERPROFILE_TYPE)


if __name__ == "__main__":
    main()
