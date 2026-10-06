from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("documents", "0003_docling_content")]

    operations = [
        migrations.AddField(
            model_name="documentcontent",
            name="layout_enc",
            field=models.BinaryField(null=True),
        ),
    ]
