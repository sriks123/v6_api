from c2corg_api.models.waypoint import WAYPOINT_TYPE
from c2corg_api.search.mapping import SearchDocument, BaseMeta


class SearchWaypoint(SearchDocument):
    class Meta(BaseMeta):
        doc_type = WAYPOINT_TYPE

    @staticmethod
    def to_search_document(document, index):
        return SearchDocument.to_search_document(document, index)