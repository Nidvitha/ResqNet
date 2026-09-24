from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inspections", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="inspection",
            name="category_data",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Verified fields specific to the report's damage category.",
            ),
        ),
    ]
