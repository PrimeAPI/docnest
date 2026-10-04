from django.db import migrations

BUCKETS = [
    ("Private", "private", "sky"),
    ("Business", "business", "violet"),
    ("Studies", "studies", "emerald"),
]
TYPES = [
    ("Mail", "mail"),
    ("Contract", "contract"),
    ("Invoice", "invoice"),
    ("Notice", "notice"),
    ("Statement", "statement"),
    ("Other", "other"),
]


def seed(apps, schema_editor):
    Bucket = apps.get_model("taxonomy", "Bucket")
    DocumentType = apps.get_model("taxonomy", "DocumentType")
    for name, slug, color in BUCKETS:
        Bucket.objects.get_or_create(slug=slug, defaults={"name": name, "color": color})
    for name, slug in TYPES:
        DocumentType.objects.get_or_create(slug=slug, defaults={"name": name})


class Migration(migrations.Migration):
    dependencies = [("taxonomy", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
