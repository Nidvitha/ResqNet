from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reports", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="disasterreport",
            name="damage_details",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Disaster and property-specific answers from the citizen report form.",
            ),
        ),
    ]