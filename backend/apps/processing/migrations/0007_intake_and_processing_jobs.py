"""Two document jobs again, split differently: intake (assemble, validate) and processing (the rest).

Before: process_document = assemble, validate, enhance; analyze_document = the rest.
After:  intake_document  = assemble, validate;          process_document = enhance and the rest.

Jobs keep working either way, because a job continues from the document's current stage.
"""

from django.db import migrations, models

INTAKE_STAGES = ("received", "assemble", "validate")
ACTIVE = ("queued", "running")


def forwards(apps, schema_editor):
    Job = apps.get_model("processing", "Job")
    # Old preparation jobs become intake jobs, unless their document already reached the
    # enhancement, which belongs to processing now.
    Job.objects.filter(kind="process_document").filter(
        models.Q(document__processing_stage__in=INTAKE_STAGES) | ~models.Q(state__in=ACTIVE)
    ).update(kind="intake_document")
    # Old analysis jobs are processing jobs. At most one active job of a kind per document:
    # where a processing job is active already, it covers the rest on its own.
    busy = set(
        Job.objects.filter(kind="process_document", state__in=ACTIVE).values_list("document_id", flat=True)
    )
    Job.objects.filter(kind="analyze_document", state__in=ACTIVE, document_id__in=busy).delete()
    Job.objects.filter(kind="analyze_document").update(kind="process_document")


class Migration(migrations.Migration):
    dependencies = [("processing", "0006_job_priority")]

    operations = [
        migrations.AlterField(
            model_name="job",
            name="kind",
            field=models.CharField(
                choices=[
                    ("intake_document", "Intake Document"),
                    ("process_document", "Process Document"),
                    ("pull_model", "Pull Model"),
                    ("delete_storage", "Delete Storage"),
                    ("train_classifier", "Train Classifier"),
                    ("reindex_document", "Reindex Document"),
                    ("backup_database", "Backup Database"),
                ],
                max_length=40,
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
