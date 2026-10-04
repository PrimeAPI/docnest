"""Taxonomy helpers: tag normalization, alias resolution and fuzzy matching."""

from __future__ import annotations

import re
import unicodedata

from django.db import IntegrityError, transaction
from rapidfuzz import fuzz, process

from apps.taxonomy.models import Correspondent, Tag, TagAlias

FUZZY_THRESHOLD = 90


def normalize_label(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    value = re.sub(r"\s+", " ", value)
    return value[:80]


def fold(value: str) -> str:
    value = normalize_label(value).casefold()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "", value)


def find_tag(name: str) -> Tag | None:
    """Resolve a tag by name, alias, folded form, or close fuzzy match."""
    name = normalize_label(name)
    if not name:
        return None
    tag = Tag.objects.filter(name__iexact=name).first()
    if tag:
        return tag
    alias = TagAlias.objects.select_related("tag").filter(alias__iexact=name).first()
    if alias:
        return alias.tag
    folded = fold(name)
    candidates: dict[str, Tag] = {}
    for t in Tag.objects.all():
        candidates[fold(t.name)] = t
    for a in TagAlias.objects.select_related("tag"):
        candidates.setdefault(fold(a.alias), a.tag)
    if folded in candidates:
        return candidates[folded]
    if candidates and len(folded) >= 4:
        match = process.extractOne(folded, list(candidates), scorer=fuzz.ratio)
        if match and match[1] >= FUZZY_THRESHOLD:
            return candidates[match[0]]
    return None


def resolve_or_create_tag(name: str, *, suggested: bool) -> Tag:
    existing = find_tag(name)
    if existing:
        return existing
    clean = normalize_label(name)
    try:
        with transaction.atomic():
            return Tag.objects.create(name=clean, is_suggested=suggested)
    except IntegrityError:
        return Tag.objects.get(name__iexact=clean)


def merge_tags(source: Tag, target: Tag) -> None:
    """Move all documents from `source` to `target`; keep source name as alias."""
    from apps.documents.models import DocumentTag

    if source.pk == target.pk:
        return
    with transaction.atomic():
        for dt in DocumentTag.objects.filter(tag=source):
            if DocumentTag.objects.filter(document_id=dt.document_id, tag=target).exists():
                dt.delete()
            else:
                dt.tag = target
                dt.save(update_fields=["tag"])
        TagAlias.objects.filter(tag=source).update(tag=target)
        source_name = source.name
        source.delete()
        if not TagAlias.objects.filter(alias__iexact=source_name).exists():
            TagAlias.objects.create(tag=target, alias=source_name)


def find_correspondent(name: str) -> Correspondent | None:
    name = normalize_label(name)
    if not name:
        return None
    c = Correspondent.objects.filter(name__iexact=name).first()
    if c:
        return c
    folded = fold(name)
    for corr in Correspondent.objects.all():
        if fold(corr.name) == folded or any(fold(a) == folded for a in corr.aliases):
            return corr
    return None
