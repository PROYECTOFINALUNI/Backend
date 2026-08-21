from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0005_migrate_approvers_to_candidates"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="approvalstep",
            name="approval_step_approver_idx",
        ),
        migrations.RemoveField(
            model_name="approvalstep",
            name="approver",
        ),
    ]
