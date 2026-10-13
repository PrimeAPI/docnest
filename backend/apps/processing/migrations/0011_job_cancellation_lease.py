import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("processing", "0010_alter_job_kind")]

    operations = [
        migrations.AddField(
            model_name="job", name="lease_token", field=models.UUIDField(default=uuid.uuid4, editable=False)
        ),
        migrations.AddField(
            model_name="job", name="cancel_requested", field=models.BooleanField(default=False)
        ),
    ]
