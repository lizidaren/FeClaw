"""
Dataset registration and query engine for structured data.
"""
from __future__ import annotations
from typing import Dict, List, Optional, Type, Any
from sqlalchemy import String, Integer, Float, Boolean, DateTime, Text
from sqlalchemy.orm import Session
from sqlalchemy import or_

from models.database import VocabularyWord


class DatasetConfig:
    """Configuration for a queryable dataset."""

    def __init__(
        self,
        model: Type,
        label: str,
        search_fields: List[str],
        filter_fields: List[str],
        sortable_fields: List[str],
        output_fields: List[str],
        identifier_field: str,
        max_limit: int = 200,
    ):
        self.model = model
        self.label = label
        self.search_fields = search_fields
        self.filter_fields = filter_fields
        self.sortable_fields = sortable_fields
        self.output_fields = output_fields
        self.identifier_field = identifier_field
        self.max_limit = max_limit

    def to_schema(self) -> dict:
        """Generate schema description for this dataset."""
        columns = self.model.__table__.columns
        fields = []
        for col_name in self.output_fields:
            col = columns.get(col_name)
            field_type = _infer_col_type(col) if col is not None else "string"
            fields.append({
                "name": col_name,
                "type": field_type,
                "searchable": col_name in self.search_fields,
                "filterable": col_name in self.filter_fields,
            })
        return {
            "search_fields": self.search_fields,
            "filterable_fields": self.filter_fields,
            "sortable_fields": self.sortable_fields,
            "fields": fields,
        }


# ── Type inference ──

_COL_TYPE_MAP = {
    String: "string",
    Text: "text",
    Integer: "integer",
    Float: "float",
    Boolean: "boolean",
    DateTime: "datetime",
}


def _infer_col_type(col) -> str:
    for sql_type, label in _COL_TYPE_MAP.items():
        if isinstance(col.type, sql_type):
            return label
    return "string"


# ── Registry ──

DATA_REGISTRY: Dict[str, DatasetConfig] = {
    "vocabulary-gaokao-3500": DatasetConfig(
        model=VocabularyWord,
        label="高考词汇",
        search_fields=["word", "meaning"],
        filter_fields=["word", "part_of_speech"],
        sortable_fields=["word", "created_at"],
        output_fields=["word", "pronunciation", "part_of_speech", "meaning"],
        identifier_field="word",
        max_limit=200,
    ),
}


def get_config(dataset: str) -> Optional[DatasetConfig]:
    return DATA_REGISTRY.get(dataset)


# ── QueryEngine ──

def _row_to_dict(item, fields: List[str]) -> dict:
    return {f: getattr(item, f, None) for f in fields}


class QueryEngine:
    """Builds and executes ORM queries from API params."""

    def __init__(self, name: str, config: DatasetConfig):
        self.name = name
        self.config = config
        self.model = config.model

    def search(
        self,
        db: Session,
        *,
        search: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
        sort: Optional[str] = None,
        order: str = "asc",
        exact_filters: Optional[Dict[str, str]] = None,
        fuzzy_filters: Optional[Dict[str, str]] = None,
    ) -> dict:
        """Execute a search query with filters, sorting, pagination.

        Args:
            exact_filters: {field: value} for exact match (WHERE field = value).
                Only fields in config.filter_fields are applied.
            fuzzy_filters: {field: value} for ILIKE match (WHERE field ILIKE '%value%').
                Any model column is allowed (not limited to search_fields).
        """
        query = db.query(self.model)

        # Full-text search (OR within each term, AND across terms)
        if search and search.strip():
            terms = search.strip().split()
            for term in terms:
                if len(term) < 2:
                    continue
                term_filters = [
                    getattr(self.model, f).ilike(f"%{term}%")
                    for f in self.config.search_fields
                ]
                query = query.filter(or_(*term_filters))

        # Exact match on filterable fields (only registered filter_fields)
        if exact_filters:
            for field in self.config.filter_fields:
                val = exact_filters.get(field)
                if val is not None and val != "":
                    query = query.filter(getattr(self.model, field) == val)

        # Fuzzy (ILIKE) match via colon syntax — any column allowed
        if fuzzy_filters:
            columns = self.model.__table__.columns
            for field, val in fuzzy_filters.items():
                if val and val.strip() and columns.get(field) is not None:
                    query = query.filter(
                        getattr(self.model, field).ilike(f"%{val.strip()}%")
                    )

        # Sorting
        if sort and sort in self.config.sortable_fields:
            col = getattr(self.model, sort)
            query = query.order_by(col.asc() if order != "desc" else col.desc())
        else:
            query = query.order_by(
                getattr(self.model, self.config.identifier_field).asc()
            )

        # Pagination
        total = query.count()
        items = query.offset((page - 1) * limit).limit(limit).all()

        return {
            "data": [_row_to_dict(item, self.config.output_fields) for item in items],
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit if limit > 0 else 0,
            "dataset": self.name,
        }

    def get_by_identifier(self, db: Session, value: str) -> Optional[dict]:
        """Get a single item by its identifier_field."""
        col = getattr(self.model, self.config.identifier_field)
        item = db.query(self.model).filter(col == value).first()
        if item is None:
            return None
        return _row_to_dict(item, self.config.output_fields)
