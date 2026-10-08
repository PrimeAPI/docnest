from __future__ import annotations

from datetime import date
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpRequest
from django.utils.text import slugify
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.analysis.analyze import DISMISSED_TAGS_KEY
from apps.analysis.series import refresh_series
from apps.audit.service import audit
from apps.documents import crypto_fields
from apps.documents.models import Document
from apps.processing.models import SystemState
from apps.taxonomy.models import Correspondent, DocumentType, Folder, MatchRule, Series, Tag, TagAlias
from apps.taxonomy.services import folder_paths, folder_subtree, merge_tags, normalize_label

router = Router(tags=["taxonomy"])

ACTIVE = Q(documents__deleted_at__isnull=True)


class NamedIn(Schema):
    name: str
    color: str | None = None


class FolderOut(Schema):
    id: int
    name: str
    parent_id: int | None
    path: str
    color: str
    document_count: int  # directly in this folder


class FolderIn(Schema):
    name: str
    parent_id: int | None = None
    color: str | None = None


class FolderPatch(Schema):
    name: str | None = None
    color: str | None = None
    parent_id: int | None = None
    move_to_root: bool = False


class TypeOut(Schema):
    id: int
    name: str
    slug: str
    document_count: int


class TagOut(Schema):
    id: int
    name: str
    color: str
    is_suggested: bool
    aliases: list[str]
    document_count: int


class TagPatch(Schema):
    name: str | None = None
    color: str | None = None
    confirm: bool = False
    aliases: list[str] | None = None


class MergeIn(Schema):
    target_id: int


class CorrespondentOut(Schema):
    id: int
    name: str
    aliases: list[str]
    document_count: int


class CorrespondentPatch(Schema):
    name: str | None = None
    aliases: list[str] | None = None


class SeriesMemberOut(Schema):
    id: UUID
    title: str
    period_label: str
    document_date: date | None
    status: str


class SeriesOut(Schema):
    id: int
    name: str
    cadence: str
    is_suggested: bool
    correspondent: str | None
    document_type: str | None
    document_count: int
    latest_date: date | None


class SeriesDetail(SeriesOut):
    members: list[SeriesMemberOut]
    missing_periods: list[str]


class SeriesPatch(Schema):
    name: str | None = None
    cadence: str | None = None
    confirm: bool = False


class SeriesIn(Schema):
    name: str
    correspondent_id: int | None = None
    document_type_id: int | None = None


class RuleOut(Schema):
    id: int
    phrase: str
    correspondent: str | None
    document_type: str | None
    tags: list[str]


class RuleIn(Schema):
    phrase: str
    correspondent_id: int | None = None
    document_type_id: int | None = None
    tag_ids: list[int] = []


def _clean_name(value: str, max_length: int = 80) -> str:
    name = normalize_label(value)[:max_length]
    if not name:
        raise HttpError(400, "Name is required")
    return name


def _unique_slug(model: type, name: str, exclude_pk: int | None = None) -> str:
    base = slugify(name)[:70] or "item"
    slug, i = base, 2
    while model.objects.filter(slug=slug).exclude(pk=exclude_pk).exists():  # type: ignore[attr-defined]
        slug, i = f"{base}-{i}", i + 1
    return slug


# --- Folders ------------------------------------------------------------------


def _folder_name(value: str) -> str:
    name = _clean_name(value)
    if "/" in name or "\\" in name:
        raise HttpError(400, "Folder names cannot contain slashes")
    return name


def _folder_out(f: Folder, n: int, paths: dict[int, str] | None = None) -> FolderOut:
    paths = paths if paths is not None else folder_paths()
    return FolderOut(
        id=f.pk,
        name=f.name,
        parent_id=f.parent_id,
        path=paths.get(f.pk, f.name),
        color=f.color,
        document_count=n,
    )


def _save_folder(f: Folder) -> None:
    try:
        with transaction.atomic():
            f.save()
    except IntegrityError as exc:
        raise HttpError(400, "A folder with this name already exists here") from exc


@router.get("/folders", response=list[FolderOut])
def list_folders(request: HttpRequest) -> list[FolderOut]:
    paths = folder_paths()
    qs = Folder.objects.annotate(n=Count("documents", filter=ACTIVE))
    return [_folder_out(f, f.n, paths) for f in qs]


@router.post("/folders", response=FolderOut)
def create_folder(request: HttpRequest, data: FolderIn) -> FolderOut:
    if data.parent_id is not None and not Folder.objects.filter(pk=data.parent_id).exists():
        raise HttpError(400, "Unknown parent folder")
    f = Folder(name=_folder_name(data.name), parent_id=data.parent_id, color=(data.color or "slate")[:20])
    _save_folder(f)
    audit("folder.created", request=request, target=f.name)
    return _folder_out(f, 0)


@router.patch("/folders/{folder_id}", response=FolderOut)
def update_folder(request: HttpRequest, folder_id: int, data: FolderPatch) -> FolderOut:
    f = Folder.objects.filter(pk=folder_id).first()
    if f is None:
        raise HttpError(404, "Not found")
    if data.name is not None:
        f.name = _folder_name(data.name)
    if data.color:
        f.color = data.color[:20]
    if data.move_to_root:
        f.parent = None
    elif data.parent_id is not None:
        if not Folder.objects.filter(pk=data.parent_id).exists():
            raise HttpError(400, "Unknown parent folder")
        if data.parent_id in folder_subtree([f.pk]):
            raise HttpError(400, "A folder cannot be moved into itself")
        f.parent_id = data.parent_id
    _save_folder(f)
    if data.parent_id is not None or data.move_to_root:
        audit("folder.moved", request=request, target=f.name)
    return _folder_out(f, f.documents.filter(deleted_at__isnull=True).count())


@router.delete("/folders/{folder_id}")
def delete_folder(request: HttpRequest, folder_id: int) -> dict[str, bool]:
    f = Folder.objects.filter(pk=folder_id).first()
    if f is None:
        raise HttpError(404, "Not found")
    if Document.objects.filter(folder_id__in=folder_subtree([f.pk]), deleted_at__isnull=True).exists():
        raise HttpError(400, "The folder (or a subfolder) still contains documents")
    f.delete()  # subfolders cascade; trashed documents become unfiled
    audit("folder.deleted", request=request, target=f.name)
    return {"ok": True}


# --- Document types ---------------------------------------------------------------


@router.get("/document-types", response=list[TypeOut])
def list_types(request: HttpRequest) -> list[TypeOut]:
    qs = DocumentType.objects.annotate(n=Count("document", filter=Q(document__deleted_at__isnull=True)))
    return [TypeOut(id=t.pk, name=t.name, slug=t.slug, document_count=t.n) for t in qs]


@router.post("/document-types", response=TypeOut)
def create_type(request: HttpRequest, data: NamedIn) -> TypeOut:
    name = _clean_name(data.name)
    try:
        t = DocumentType.objects.create(name=name, slug=_unique_slug(DocumentType, name))
    except IntegrityError as exc:
        raise HttpError(400, "A document type with this name exists") from exc
    return TypeOut(id=t.pk, name=t.name, slug=t.slug, document_count=0)


@router.patch("/document-types/{type_id}", response=TypeOut)
def update_type(request: HttpRequest, type_id: int, data: NamedIn) -> TypeOut:
    t = DocumentType.objects.filter(pk=type_id).first()
    if t is None:
        raise HttpError(404, "Not found")
    t.name = _clean_name(data.name)
    try:
        t.save()
    except IntegrityError as exc:
        raise HttpError(400, "A document type with this name exists") from exc
    return TypeOut(id=t.pk, name=t.name, slug=t.slug, document_count=t.document_set.count())


@router.delete("/document-types/{type_id}")
def delete_type(request: HttpRequest, type_id: int) -> dict[str, bool]:
    deleted, _ = DocumentType.objects.filter(pk=type_id).delete()
    if not deleted:
        raise HttpError(404, "Not found")
    return {"ok": True}


# --- Tags -----------------------------------------------------------------------


def _tag_out(t: Tag, n: int | None = None) -> TagOut:
    return TagOut(
        id=t.pk,
        name=t.name,
        color=t.color,
        is_suggested=t.is_suggested,
        aliases=sorted(a.alias for a in t.aliases.all()),
        document_count=n if n is not None else t.documents.filter(deleted_at__isnull=True).count(),
    )


@router.get("/tags", response=list[TagOut])
def list_tags(request: HttpRequest) -> list[TagOut]:
    qs = Tag.objects.prefetch_related("aliases").annotate(n=Count("documents", filter=ACTIVE, distinct=True))
    return [_tag_out(t, t.n) for t in qs]


@router.post("/tags", response=TagOut)
def create_tag(request: HttpRequest, data: NamedIn) -> TagOut:
    name = _clean_name(data.name)
    if Tag.objects.filter(name__iexact=name).exists() or TagAlias.objects.filter(alias__iexact=name).exists():
        raise HttpError(400, "This tag (or an alias of it) already exists")
    t = Tag.objects.create(name=name, color=data.color or "slate")
    return _tag_out(t, 0)


@router.patch("/tags/{tag_id}", response=TagOut)
def update_tag(request: HttpRequest, tag_id: int, data: TagPatch) -> TagOut:
    t = Tag.objects.filter(pk=tag_id).first()
    if t is None:
        raise HttpError(404, "Not found")
    with transaction.atomic():
        if data.name is not None:
            new = _clean_name(data.name)
            if new.lower() != t.name.lower():
                if Tag.objects.filter(name__iexact=new).exclude(pk=t.pk).exists():
                    raise HttpError(400, "Another tag has this name — merge them instead")
                TagAlias.objects.filter(alias__iexact=new).delete()
                # keep the old name as alias so future matches land on this tag
                if not TagAlias.objects.filter(alias__iexact=t.name).exists():
                    TagAlias.objects.create(tag=t, alias=t.name)
            t.name = new
        if data.color:
            t.color = data.color[:20]
        if data.confirm:
            t.is_suggested = False
        t.save()
        if data.aliases is not None:
            TagAlias.objects.filter(tag=t).delete()
            for alias in {normalize_label(a) for a in data.aliases if normalize_label(a)}:
                if alias.lower() == t.name.lower():
                    continue
                if Tag.objects.filter(name__iexact=alias).exists():
                    raise HttpError(400, f"'{alias}' is already a tag — merge it instead")
                TagAlias.objects.filter(alias__iexact=alias).delete()
                TagAlias.objects.create(tag=t, alias=alias)
    return _tag_out(t)


@router.post("/tags/{tag_id}/merge", response=TagOut)
def merge_tag(request: HttpRequest, tag_id: int, data: MergeIn) -> TagOut:
    source = Tag.objects.filter(pk=tag_id).first()
    target = Tag.objects.filter(pk=data.target_id).first()
    if source is None or target is None:
        raise HttpError(404, "Not found")
    merge_tags(source, target)
    target.is_suggested = False
    target.save(update_fields=["is_suggested"])
    audit("tag.merged", request=request, target=target.name, source=source.name)
    return _tag_out(target)


@router.delete("/tags/{tag_id}")
def delete_tag(request: HttpRequest, tag_id: int) -> dict[str, bool]:
    t = Tag.objects.filter(pk=tag_id).first()
    if t is None:
        raise HttpError(404, "Not found")
    names = [t.name, *(a.alias for a in t.aliases.all())]
    # Remember deleted names so the built-in keyword tagging does not recreate them.
    row, _ = SystemState.objects.get_or_create(key=DISMISSED_TAGS_KEY, defaults={"value": {"names": []}})
    row.value = {"names": sorted(set(row.value.get("names", [])) | set(names))}
    row.save()
    t.delete()
    audit("tag.deleted", request=request, target=names[0])
    return {"ok": True}


# --- Correspondents ---------------------------------------------------------------


@router.get("/correspondents", response=list[CorrespondentOut])
def list_correspondents(request: HttpRequest) -> list[CorrespondentOut]:
    qs = Correspondent.objects.annotate(n=Count("document", filter=Q(document__deleted_at__isnull=True)))
    return [CorrespondentOut(id=c.pk, name=c.name, aliases=c.aliases, document_count=c.n) for c in qs]


@router.post("/correspondents", response=CorrespondentOut)
def create_correspondent(request: HttpRequest, data: NamedIn) -> CorrespondentOut:
    name = _clean_name(data.name, 150)
    try:
        c = Correspondent.objects.create(name=name)
    except IntegrityError as exc:
        raise HttpError(400, "This correspondent exists") from exc
    return CorrespondentOut(id=c.pk, name=c.name, aliases=[], document_count=0)


@router.patch("/correspondents/{corr_id}", response=CorrespondentOut)
def update_correspondent(request: HttpRequest, corr_id: int, data: CorrespondentPatch) -> CorrespondentOut:
    c = Correspondent.objects.filter(pk=corr_id).first()
    if c is None:
        raise HttpError(404, "Not found")
    if data.name is not None:
        new = _clean_name(data.name, 150)
        if new != c.name and c.name not in c.aliases:
            c.aliases = [*c.aliases, c.name]
        c.name = new
    if data.aliases is not None:
        c.aliases = sorted({normalize_label(a)[:150] for a in data.aliases if normalize_label(a)} - {c.name})
    try:
        c.save()
    except IntegrityError as exc:
        raise HttpError(400, "Another correspondent has this name — merge them instead") from exc
    return CorrespondentOut(id=c.pk, name=c.name, aliases=c.aliases, document_count=c.document_set.count())


@router.post("/correspondents/{corr_id}/merge", response=CorrespondentOut)
def merge_correspondent(request: HttpRequest, corr_id: int, data: MergeIn) -> CorrespondentOut:
    source = Correspondent.objects.filter(pk=corr_id).first()
    target = Correspondent.objects.filter(pk=data.target_id).first()
    if source is None or target is None or source.pk == target.pk:
        raise HttpError(404, "Not found")
    with transaction.atomic():
        Document.objects.filter(correspondent=source).update(correspondent=target)
        Series.objects.filter(correspondent=source).update(correspondent=target)
        target.aliases = sorted(set(target.aliases) | {source.name} | set(source.aliases))
        target.save()
        source.delete()
    return CorrespondentOut(
        id=target.pk, name=target.name, aliases=target.aliases, document_count=target.document_set.count()
    )


@router.delete("/correspondents/{corr_id}")
def delete_correspondent(request: HttpRequest, corr_id: int) -> dict[str, bool]:
    deleted, _ = Correspondent.objects.filter(pk=corr_id).delete()
    if not deleted:
        raise HttpError(404, "Not found")
    return {"ok": True}


# --- Series -------------------------------------------------------------------------


def _series_out(s: Series, n: int, latest: date | None) -> SeriesOut:
    return SeriesOut(
        id=s.pk,
        name=s.name,
        cadence=s.cadence,
        is_suggested=s.is_suggested,
        correspondent=s.correspondent.name if s.correspondent else None,
        document_type=s.document_type.name if s.document_type else None,
        document_count=n,
        latest_date=latest,
    )


def _missing_periods(dates: list[date], cadence: str) -> list[str]:
    from apps.analysis.series import period_label

    if cadence != Series.Cadence.MONTHLY or len(dates) < 2:
        return []
    present = {(d.year, d.month) for d in dates}
    start, end = min(dates), max(dates)
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        if (y, m) not in present:
            out.append(period_label(date(y, m, 1), cadence))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out[:24]


@router.get("/series", response=list[SeriesOut])
def list_series(request: HttpRequest) -> list[SeriesOut]:
    from django.db.models import Max

    qs = Series.objects.select_related("correspondent", "document_type").annotate(
        n=Count("documents", filter=ACTIVE), latest=Max("documents__document_date")
    )
    return [_series_out(s, s.n, s.latest) for s in qs if s.n > 0 or not s.is_suggested]


@router.post("/series", response=SeriesOut)
def create_series(request: HttpRequest, data: SeriesIn) -> SeriesOut:
    s = Series.objects.create(
        name=_clean_name(data.name, 150),
        correspondent_id=data.correspondent_id,
        document_type_id=data.document_type_id,
    )
    return _series_out(s, 0, None)


@router.get("/series/{series_id}", response=SeriesDetail)
def series_detail(request: HttpRequest, series_id: int) -> SeriesDetail:
    s = Series.objects.select_related("correspondent", "document_type").filter(pk=series_id).first()
    if s is None:
        raise HttpError(404, "Not found")
    members = list(s.documents.filter(deleted_at__isnull=True).order_by("-document_date", "-uploaded_at"))
    dates = [m.document_date for m in members if m.document_date]
    base = _series_out(s, len(members), max(dates) if dates else None)
    return SeriesDetail(
        **base.dict(),
        members=[
            SeriesMemberOut(
                id=m.uuid,
                title=crypto_fields.get_title(m),
                period_label=m.period_label,
                document_date=m.document_date,
                status=m.status,
            )
            for m in members
        ],
        missing_periods=_missing_periods(dates, s.cadence),
    )


@router.patch("/series/{series_id}", response=SeriesOut)
def update_series(request: HttpRequest, series_id: int, data: SeriesPatch) -> SeriesOut:
    s = Series.objects.filter(pk=series_id).first()
    if s is None:
        raise HttpError(404, "Not found")
    if data.name is not None:
        s.name = _clean_name(data.name, 150)
        s.is_suggested = False
    if data.cadence is not None:
        if data.cadence not in Series.Cadence.values:
            raise HttpError(400, "Invalid cadence")
        s.cadence = data.cadence
    if data.confirm:
        s.is_suggested = False
    s.save()
    if data.cadence is not None:
        from apps.analysis.series import period_label

        for m in s.documents.all():
            Document.objects.filter(pk=m.pk).update(period_label=period_label(m.document_date, s.cadence))
    else:
        refresh_series(s)
    s.refresh_from_db()
    return _series_out(s, s.documents.count(), None)


@router.delete("/series/{series_id}")
def delete_series(request: HttpRequest, series_id: int) -> dict[str, bool]:
    s = Series.objects.filter(pk=series_id).first()
    if s is None:
        raise HttpError(404, "Not found")
    Document.objects.filter(series=s).update(period_label="")
    s.delete()
    return {"ok": True}


# --- Match rules ------------------------------------------------------------------------


def _rule_out(r: MatchRule) -> RuleOut:
    return RuleOut(
        id=r.pk,
        phrase=r.phrase,
        correspondent=r.correspondent.name if r.correspondent else None,
        document_type=r.document_type.name if r.document_type else None,
        tags=[t.name for t in r.tags.all()],
    )


@router.get("/rules", response=list[RuleOut])
def list_rules(request: HttpRequest) -> list[RuleOut]:
    return [
        _rule_out(r)
        for r in MatchRule.objects.select_related("correspondent", "document_type").prefetch_related("tags")
    ]


@router.post("/rules", response=RuleOut)
def create_rule(request: HttpRequest, data: RuleIn) -> RuleOut:
    phrase = normalize_label(data.phrase)[:200]
    if len(phrase) < 3:
        raise HttpError(400, "The phrase must have at least 3 characters")
    if not (data.correspondent_id or data.document_type_id or data.tag_ids):
        raise HttpError(400, "A rule must set a correspondent, a type or tags")
    r = MatchRule.objects.create(
        phrase=phrase, correspondent_id=data.correspondent_id, document_type_id=data.document_type_id
    )
    r.tags.set(Tag.objects.filter(pk__in=data.tag_ids))
    return _rule_out(r)


@router.delete("/rules/{rule_id}")
def delete_rule(request: HttpRequest, rule_id: int) -> dict[str, bool]:
    deleted, _ = MatchRule.objects.filter(pk=rule_id).delete()
    if not deleted:
        raise HttpError(404, "Not found")
    return {"ok": True}
