import pytest
from django.db import connection

from apps.documents.models import Document
from apps.search import tokenizer
from apps.search.index import IndexInput, index_document, search
from apps.search.models import SearchTerm
from apps.search.snippets import make_snippet
from apps.taxonomy.models import Bucket

pytestmark = pytest.mark.django_db


def make_doc(content: str, title: str = "", bucket: str = "private") -> Document:
    import uuid

    doc = Document.objects.create(
        bucket=Bucket.objects.get(slug=bucket), content_hash=uuid.uuid4().hex, processing_state="done"
    )
    index_document(doc.pk, IndexInput(title=title, content=content))
    return doc


def ids(query: str, qs=None) -> list[int]:
    qs = qs if qs is not None else Document.objects.all()
    return search(query, qs, limit=50, offset=0).ids


def test_tokenizer_folds_and_stems():
    assert tokenizer.words("Fahrzeugversicherung, KFZ!") == ["fahrzeugversicherung", "kfz"]
    assert "rechnung" in tokenizer.stems("rechnungen")
    assert tokenizer.fold("gebühr") == "gebuehr"


def test_exact_stem_prefix_and_compound_matches():
    car = make_doc("Ihre Fahrzeugversicherung für das Jahr 2026")
    power = make_doc("Stromrechnung für Februar")
    other = make_doc("Einladung zum Sommerfest")
    assert ids("Fahrzeugversicherung") == [car.pk]
    assert ids("fahrzeugvers") == [car.pk]  # prefix
    assert ids("Versicherung") == [car.pk]  # compound tail
    assert set(ids("Rechnung")) == {power.pk}  # compound tail of Stromrechnung
    assert ids("Rechnungen") == [power.pk]  # stem
    assert other.pk not in ids("Versicherung")


def test_umlaut_folding():
    doc = make_doc("Gebührenbescheid über Müllabfuhr")
    assert ids("Muellabfuhr") == [doc.pk]
    assert ids("müllabfuhr") == [doc.pk]
    assert ids("gebuehrenbescheid") == [doc.pk]


def test_all_words_must_match_and_filters_apply():
    a = make_doc("Rechnung Fahrzeug Werkstatt", bucket="private")
    b = make_doc("Rechnung Büromaterial", bucket="business")
    assert ids("rechnung fahrzeug") == [a.pk]
    assert set(ids("rechnung")) == {a.pk, b.pk}
    assert ids("rechnung", Document.objects.filter(bucket__slug="business")) == [b.pk]


def test_title_ranks_higher_than_content():
    content_only = make_doc("Erwähnung: Steuerbescheid liegt bei", title="Brief")
    in_title = make_doc("Festsetzung der Einkommensteuer", title="Steuerbescheid 2025")
    assert ids("Steuerbescheid")[0] == in_title.pk
    assert content_only.pk in ids("Steuerbescheid")


def test_index_contains_no_plaintext():
    make_doc("Geheimnisvolle Kontonummer Zuckerwatte", title="Vertraulich")
    with connection.cursor() as cursor:
        cursor.execute("SELECT term::text, field, tf FROM search_searchterm")
        dump = " ".join(" ".join(map(str, row)) for row in cursor.fetchall())
    assert "zuckerwatte" not in dump.lower()
    assert SearchTerm.objects.count() > 0


def test_reindex_is_idempotent():
    doc = make_doc("Kontoauszug Januar")
    n = SearchTerm.objects.filter(document=doc).count()
    index_document(doc.pk, IndexInput(content="Kontoauszug Januar"))
    assert SearchTerm.objects.filter(document=doc).count() == n


def test_pagination_total():
    for i in range(7):
        make_doc(f"Mahnung Nummer {i}")
    hits = search("Mahnung", Document.objects.all(), limit=3, offset=3)
    assert hits.total == 7 and len(hits.ids) == 3
    beyond = search("Mahnung", Document.objects.all(), limit=3, offset=30)
    assert beyond.total == 7 and beyond.ids == []


def test_snippet_highlights_match():
    text = "Sehr geehrter Kunde,\nanbei Ihre Fahrzeugversicherung für 2026.\nMit freundlichen Grüßen"
    snip = make_snippet(text, "versicherung")
    assert snip is not None
    start, end = snip.highlights[0]
    assert "versicherung" in snip.text[start:end].lower()
