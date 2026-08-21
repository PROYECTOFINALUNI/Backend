from django.db import transaction

from common.api import DomainConflict
from expenses.models import ExpenseReport
from users.models import User


@transaction.atomic
def create_report(*, actor, title):
    return ExpenseReport.objects.create(user=actor, title=title)


@transaction.atomic
def update_report(*, report_id, actor, data):
    report = ExpenseReport.objects.select_for_update().get(pk=report_id)
    if report.status != ExpenseReport.Status.DRAFT or (
        report.user_id != actor.id and actor.role != User.Role.ADMIN
    ):
        raise DomainConflict(
            "report_not_editable",
            "Only a draft report's owner or an admin may edit it.",
        )
    if "title" in data:
        report.title = data["title"]
        report.save(update_fields=["title", "updated_at"])
    return report


@transaction.atomic
def delete_report(*, report_id, actor):
    report = ExpenseReport.objects.select_for_update().get(pk=report_id)
    allowed = report.user_id == actor.id or actor.role == User.Role.ADMIN
# Solo se pueden eliminar informes en borrador que nunca hayan sido enviados ni tengan eventos de aprobación.
    if (
        not allowed
        or report.status != report.Status.DRAFT
        or report.submitted_at is not None
        or report.approval_events.exists()
    ):
        raise DomainConflict("report_not_deletable", "The report cannot be deleted.")
    report.delete()
