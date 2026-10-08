from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("taxonomy", "0003_folder"),
        ("documents", "0006_buckets_to_folders"),
    ]

    operations = [migrations.DeleteModel(name="Bucket")]
