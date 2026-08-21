import uuid

from django.db import migrations


def approvers_to_candidates(apps, schema_editor):
    ApprovalStep = apps.get_model("approvals", "ApprovalStep")
    ApprovalStepCandidate = apps.get_model("approvals", "ApprovalStepCandidate")
    ApprovalStepCandidate.objects.bulk_create(
        ApprovalStepCandidate(id=uuid.uuid4(), step_id=step_id, user_id=approver_id)
        for step_id, approver_id in ApprovalStep.objects.values_list("id", "approver_id")
    )


def candidates_to_approvers(apps, schema_editor):
    ApprovalStep = apps.get_model("approvals", "ApprovalStep")
    ApprovalStepCandidate = apps.get_model("approvals", "ApprovalStepCandidate")
    for step in ApprovalStep.objects.all():
        candidate = ApprovalStepCandidate.objects.filter(step_id=step.id).first()
        if candidate is not None:
            step.approver_id = candidate.user_id
            step.save(update_fields=["approver"])


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0004_approval_rule_relations"),
    ]

    operations = [
        migrations.RunPython(approvers_to_candidates, candidates_to_approvers),
    ]
