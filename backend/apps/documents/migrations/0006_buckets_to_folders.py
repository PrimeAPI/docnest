import django.db.models.deletion
from django.db import migrations, models


def _flush_constraint_checks(schema_editor):
    """Run deferred FK checks now: PostgreSQL refuses ALTER TABLE while row updates have pending checks."""
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def buckets_to_folders(apps, schema_editor):
    """Every bucket becomes a top-level folder; documents keep their place."""
    Bucket = apps.get_model("taxonomy", "Bucket")
    Folder = apps.get_model("taxonomy", "Folder")
    Document = apps.get_model("documents", "Document")
    for bucket in Bucket.objects.all():
        folder = Folder.objects.filter(parent=None, name__iexact=bucket.name).first()
        if folder is None:
            folder = Folder.objects.create(name=bucket.name, color=bucket.color)
        Document.objects.filter(bucket=bucket).update(folder=folder)
    for document in Document.objects.exclude(field_sources={}).only("pk", "field_sources"):
        sources = dict(document.field_sources)
        if "bucket" in sources:
            sources["folder"] = sources.pop("bucket")
            Document.objects.filter(pk=document.pk).update(field_sources=sources)
    _flush_constraint_checks(schema_editor)


def folders_to_buckets(apps, schema_editor):
    from django.utils.text import slugify

    Bucket = apps.get_model("taxonomy", "Bucket")
    Folder = apps.get_model("taxonomy", "Folder")
    Document = apps.get_model("documents", "Document")
    fallback = None
    for folder in Folder.objects.filter(parent=None):
        bucket, _ = Bucket.objects.get_or_create(
            slug=slugify(folder.name)[:70] or f"folder-{folder.pk}",
            defaults={"name": folder.name, "color": folder.color},
        )
        fallback = fallback or bucket
        Document.objects.filter(folder=folder).update(bucket=bucket)
    if Document.objects.filter(bucket=None).exists():
        fallback = Bucket.objects.filter(slug="private").first() or fallback
        if fallback is None:
            fallback = Bucket.objects.create(name="Private", slug="private")
        Document.objects.filter(bucket=None).update(bucket=fallback)
    _flush_constraint_checks(schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0005_assemble_stage"),
        ("taxonomy", "0003_folder"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="folder",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="documents",
                to="taxonomy.folder",
            ),
        ),
        migrations.AlterField(
            model_name="document",
            name="bucket",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="documents",
                to="taxonomy.bucket",
            ),
        ),
        migrations.RunPython(buckets_to_folders, folders_to_buckets),
        migrations.RemoveField(model_name="document", name="bucket"),
    ]
