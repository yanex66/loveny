from django.db import migrations


RETIRED_COLUMNS = {
    'dating_profile': (
        'has_used_free_sex_call',
        'is_dnd',
        'is_host_ready',
        'response_rate',
        'total_calls_completed',
    ),
    'dating_siteconfiguration': (
        'call_rate_per_minute',
        'grace_period_seconds',
        'min_coins_for_video_call',
    ),
}


def remove_retired_columns(apps, schema_editor):
    quote_name = schema_editor.connection.ops.quote_name
    with schema_editor.connection.cursor() as cursor:
        for table, retired_columns in RETIRED_COLUMNS.items():
            existing_columns = {
                column.name
                for column in schema_editor.connection.introspection.get_table_description(
                    cursor,
                    table,
                )
            }
            for column in retired_columns:
                if column in existing_columns:
                    schema_editor.execute(
                        f'ALTER TABLE {quote_name(table)} DROP COLUMN {quote_name(column)}'
                    )


class Migration(migrations.Migration):

    dependencies = [
        ('dating', '0053_alter_supportknowledgebase_category'),
    ]

    operations = [
        migrations.RunPython(remove_retired_columns, migrations.RunPython.noop),
    ]
