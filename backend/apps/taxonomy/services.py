"""Taxonomy helpers: tag normalization, alias resolution, fuzzy matching and folder paths."""

from __future__ import annotations

import re
import unicodedata

from django.db import IntegrityError, transaction
from rapidfuzz import fuzz, process

from apps.taxonomy.models import Correspondent, Folder, Tag, TagAlias

FUZZY_THRESHOLD = 90
MAX_FOLDER_DEPTH = 10


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


MAX_LEARNED_ALIASES = 30


def learn_alias(correspondent: Correspondent, read_sender: str | None, *, document_id: int) -> bool:
    """Remember how a sender was read when the user filed the document under `correspondent`.

    The next letter read the same way ("RWM GmbH" from a payslip's return address,
    "Versicherer im Raum der Kirchen" for "VRK Sachversicherung") is then filed the same
    way. Nothing is learned when that reading names another correspondent that still has
    documents of its own: then it is a different sender, not a different spelling.
    """
    sender = normalize_label(read_sender or "")
    folded = fold(sender)
    if len(folded) < 3 or len(correspondent.aliases) >= MAX_LEARNED_ALIASES:
        return False
    if folded in {fold(correspondent.name), *(fold(a) for a in correspondent.aliases)}:
        return False
    other = find_correspondent(sender)
    if other is not None and other.pk != correspondent.pk:
        from apps.documents.models import Document

        if Document.objects.filter(correspondent=other).exclude(pk=document_id).exists():
            return False
        # Created from the misreading and now empty: it would keep winning by name.
        if not other.series_set.exists() and not other.matchrule_set.exists():
            other.delete()
    correspondent.aliases = sorted({*correspondent.aliases, sender[:150]})
    correspondent.save(update_fields=["aliases"])
    return True


# --- Folders --------------------------------------------------------------------


def split_folder_path(path: str) -> list[str]:
    """Split "Private/Taxes/2024" into cleaned folder names (empty segments are dropped)."""
    names = [normalize_label(part) for part in path.replace("\\", "/").split("/")]
    names = [n for n in names if n]
    if len(names) > MAX_FOLDER_DEPTH:
        raise ValueError(f"Folder paths can be at most {MAX_FOLDER_DEPTH} levels deep")
    return names


def find_folder(names: list[str]) -> Folder | None:
    folder: Folder | None = None
    for name in names:
        folder = Folder.objects.filter(parent=folder, name__iexact=name).first()
        if folder is None:
            return None
    return folder


def ensure_folder_path(path: str) -> Folder | None:
    """Resolve a folder path case-insensitively, creating missing folders. Empty path = no folder."""
    folder: Folder | None = None
    for name in split_folder_path(path):
        existing = Folder.objects.filter(parent=folder, name__iexact=name).first()
        if existing is None:
            try:
                with transaction.atomic():
                    existing = Folder.objects.create(parent=folder, name=name)
            except IntegrityError:  # created concurrently
                existing = Folder.objects.get(parent=folder, name__iexact=name)
        folder = existing
    return folder


def folder_paths() -> dict[int, str]:
    """Full display path ("Private / Taxes") of every folder, keyed by id."""
    rows = {pk: (name, parent) for pk, name, parent in Folder.objects.values_list("pk", "name", "parent_id")}
    paths: dict[int, str] = {}

    def resolve(pk: int) -> str:
        if pk not in paths:
            name, parent = rows[pk]
            paths[pk] = f"{resolve(parent)} / {name}" if parent in rows else name
        return paths[pk]

    for pk in rows:
        resolve(pk)
    return paths


def folder_subtree(folder_ids: list[int]) -> set[int]:
    """The given folders plus all their descendants."""
    children: dict[int | None, list[int]] = {}
    for pk, parent in Folder.objects.values_list("pk", "parent_id"):
        children.setdefault(parent, []).append(pk)
    found: set[int] = set()
    stack = list(folder_ids)
    while stack:
        pk = stack.pop()
        if pk in found:
            continue
        found.add(pk)
        stack.extend(children.get(pk, []))
    return found
