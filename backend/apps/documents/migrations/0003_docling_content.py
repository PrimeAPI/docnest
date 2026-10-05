from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("documents", "0002_initial")]

    operations = [
        migrations.AddField(
            model_name="document",
            name="ocr_backend",
            field=models.CharField(
                blank=True,
                choices=[("ocrmypdf", "OCRmyPDF / Tesseract"), ("docling", "Docling")],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="documentcontent",
            name="format",
            field=models.CharField(default="text", max_length=20),
        ),
        migrations.AddField(
            model_name="documentcontent",
            name="structured_enc",
            field=models.BinaryField(null=True),
        ),
    ]
