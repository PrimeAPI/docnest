import django.db.models.deletion
import django.db.models.functions.text
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("taxonomy", "0002_seed")]

    operations = [
        migrations.CreateModel(
            name="Folder",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=80)),
                ("color", models.CharField(default="slate", max_length=20)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "parent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="children",
                        to="taxonomy.folder",
                    ),
                ),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddConstraint(
            model_name="folder",
            constraint=models.UniqueConstraint(
                models.F("parent"),
                django.db.models.functions.text.Lower("name"),
                condition=models.Q(("parent__isnull", False)),
                name="folder_name_ci_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="folder",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("name"),
                condition=models.Q(("parent__isnull", True)),
                name="folder_root_name_ci_unique",
            ),
        ),
    ]
