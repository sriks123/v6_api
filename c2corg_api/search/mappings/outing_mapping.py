from c2corg_api.models.outing import OUTING_TYPE
from c2corg_api.search.mapping import SearchDocument, BaseMeta


class SearchOuting(SearchDocument):
    class Meta(BaseMeta):
        doc_type = OUTING_TYPE

    @staticmethod
    def to_search_document(document, index):
        return SearchDocument.to_search_document(document, index)