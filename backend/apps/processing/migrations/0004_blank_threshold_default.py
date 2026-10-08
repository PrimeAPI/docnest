"""Blank pages are measured more precisely now (edges, punch holes and dust are ignored), so the
default threshold drops from 0.01 % to 0.003 % ink. Installations that saved the settings with the
old default follow along; a value someone chose deliberately stays."""

from django.db import migrations

OLD_DEFAULT, NEW_DEFAULT = 0.01, 0.003


def forwards(apps, schema_editor):
    SystemState = apps.get_model("processing", "SystemState")
    for state in SystemState.objects.filter(key="scan_enhancement"):
        value = state.value
        if isinstance(value, dict) and value.get("blank_threshold") == OLD_DEFAULT:
            state.value = {**value, "blank_threshold": NEW_DEFAULT}
            state.save(update_fields=["value"])


class Migration(migrations.Migration):
    dependencies = [
        ("processing", "0003_job_backup_database"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
