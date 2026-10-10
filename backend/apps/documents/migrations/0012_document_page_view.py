from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("documents", "0011_alteration_documentpage")]

    operations = [
        migrations.AddField(
            model_name="document",
            name="page_view",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name="alteration",
            name="kind",
            field=models.CharField(
                choices=[
                    ("compose", "Compose"),
                    ("trash", "Trash"),
                    ("restore", "Restore"),
                    ("edit", "Edit"),
                    ("view", "View"),
                ],
                max_length=10,
            ),
        ),
    ]
